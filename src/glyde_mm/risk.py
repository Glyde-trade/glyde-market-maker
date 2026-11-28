"""Local pre-trade limits. The venue remains the final authority for every order."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .client import (
    ActionRefused,
    EngineUnavailable,
    RateLimited,
    StreamDisconnected,
    engine_unavailable,
    reconnectable,
)
from .config import RiskConfig
from .live_state import terminal_order_status
from .models import (
    CancelRequest,
    CancelResult,
    OrderResult,
    bot_client_order_id,
    is_bot_order,
)
from .risk_state import RiskStateStore

if TYPE_CHECKING:
    from .metrics import Metrics
    from .models import (
        Account,
        Book,
        HistoricalOrder,
        OpenOrder,
        OrderRequest,
        TradingClient,
    )

_LOG = logging.getLogger("glyde_mm")


PROOF_RETRY_INITIAL_SECONDS = 0.25
PROOF_RETRY_MAX_SECONDS = 5.0


class RiskViolation(RuntimeError):
    """A quote was refused before dispatch; the message contains no credentials."""


class StaleMarket(RiskViolation):
    def __init__(self, symbol: str, reason: str) -> None:
        super().__init__(reason)
        self.symbol = symbol


class VenueSweepDeferred(StreamDisconnected):
    """Every known original is confirmed closed; only the venue sweep awaits a fresh read."""


class StaleAccount(StreamDisconnected):
    """The account read is older than account_age_seconds: pull quotes and resnapshot.

    A drop, not a risk stop (GLYDE-159 account resnapshots stall 40-120s); the runner's
    reconnect window still ends the run if the account stays stale.
    """

    def __init__(self) -> None:
        super().__init__("stale_account")


@dataclass(frozen=True)
class RiskLimits:
    max_position: Decimal = Decimal("0.001")
    max_order_notional: Decimal = Decimal("10")
    max_daily_loss: Decimal = Decimal("5")
    stale_book_seconds: float = 3.0
    account_age_seconds: float = 10.0
    max_orders_per_minute: int = 60
    max_total_notional: Decimal | None = None

    def __post_init__(self) -> None:
        if (
            type(self.max_orders_per_minute) is not int
            or not 1 <= self.max_orders_per_minute <= 6000
        ):
            raise ValueError("max_orders_per_minute must be an integer in 1..6000")
        for name in ("max_position", "max_order_notional", "max_daily_loss"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        if self.max_total_notional is not None and not _positive(self.max_total_notional):
            raise ValueError("max_total_notional must be positive")
        for value in (self.stale_book_seconds, self.account_age_seconds):
            if not isinstance(value, (int, float)) or not 0 < value <= 3600:
                raise ValueError("freshness limits must be positive and at most 3600 seconds")

    @classmethod
    def from_config(cls, config: RiskConfig) -> RiskLimits:
        return cls(
            max_position=config.max_position,
            max_order_notional=config.max_order_notional,
            max_daily_loss=config.max_daily_loss,
            max_total_notional=config.max_total_notional,
            max_orders_per_minute=config.max_orders_per_minute,
            stale_book_seconds=float(config.stale_book_seconds),
            account_age_seconds=float(config.account_age_seconds),
        )


def venue_proves_closed(
    requests: Sequence[CancelRequest],
    open_orders: Sequence[OpenOrder],
    history: Sequence[HistoricalOrder],
) -> bool:
    """True when no request is open and each has a terminal historicalOrders status."""

    def matches(request: CancelRequest, order: OpenOrder | HistoricalOrder) -> bool:
        cloid = request.client_order_id
        return order.symbol == request.symbol and (
            (request.order_id is not None and order.order_id == request.order_id)
            or (cloid is not None and (order.client_order_id or "").lower() == cloid.lower())
        )

    def closed(request: CancelRequest) -> bool:
        return not any(matches(request, order) for order in open_orders) and any(
            matches(request, order) and terminal_order_status(order.status) for order in history
        )

    return all(closed(request) for request in requests)


def _positive(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


class RiskGuard:
    """Tracks both observed and in-flight orders, including ambiguous submissions."""

    def __init__(
        self,
        limits: RiskLimits,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        state_path: Path | None = None,
        reference_check: Callable[[Book], Decimal] | None = None,
        position_limits: dict[str, Decimal] | None = None,
        simulated: bool = False,
    ) -> None:
        self.limits = limits
        self.position_limits = dict(position_limits or {})
        if any(not symbol or not _positive(cap) for symbol, cap in self.position_limits.items()):
            raise ValueError("invalid market position limits")
        self.clock = clock
        self.wall_clock = wall_clock
        self.reference_check = reference_check
        self.state_path = state_path
        self.books: dict[str, Book] = {}
        self.account_state: Account | None = None
        self.pending: dict[str, OrderRequest] = {}
        self.open_orders: list[OpenOrder | OrderRequest] = []
        self.halted: str | None = None
        self._day: str | None = None
        self._equity_start: Decimal | None = None
        self._execution: dict[str, Any] | None = None
        self._loss_halted_at_load = False
        self._order_attempts: list[float] = []
        self._last_rate_clock: float | None = None
        self._rate_monotonic: list[float] = []
        if state_path is not None and state_path.exists():
            try:
                data = json.loads(state_path.read_text())
                if data["day"] is not None or data["equity_start"] is not None:
                    self._day = datetime.strptime(data["day"], "%Y-%m-%d").date().isoformat()
                    self._equity_start = Decimal(data["equity_start"])
                    if not _positive(self._equity_start):
                        raise ValueError("invalid risk baseline")
                if not isinstance(data["loss_halted"], bool) or (
                    data["loss_halted"] and self._equity_start is None
                ):
                    raise ValueError("invalid risk state")
                self._order_attempts = data["order_attempts"]
                if (
                    not isinstance(self._order_attempts, list)
                    or len(self._order_attempts) > 6000
                    or any(
                        type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0
                        for stamp in self._order_attempts
                    )
                    or self._order_attempts != sorted(self._order_attempts)
                    or (self._order_attempts and self._order_attempts[-1] > self.wall_clock())
                ):
                    raise ValueError("invalid order rate state")
                wall, monotonic_now = self.wall_clock(), self.clock()
                self._rate_monotonic = [
                    monotonic_now - min(60, wall - stamp) for stamp in self._order_attempts
                ]
                self._execution = data["execution"]
                self._loss_halted_at_load = data["loss_halted"]
                if self._execution is not None:
                    self._validate_execution(self._execution)
                    # Simulated orders died with the prior process; no venue holds an outcome.
                    # An empty cancel latch names no original, so nothing could ever resolve it.
                    if simulated or (
                        self._execution["reason"] == "cancel_outcome_unknown"
                        and not self._execution["originals"]
                    ):
                        self._execution = None
                if self._execution is not None:
                    self.halted = self._execution["reason"]
                elif data["loss_halted"]:
                    self.halted = "daily_loss"
            except (ValueError, KeyError, TypeError, OSError) as exc:
                raise RiskViolation("risk_state_unavailable") from exc

        self._state_store = RiskStateStore(state_path) if state_path is not None else None

    @staticmethod
    def _validate_execution(record: dict[str, Any]) -> None:
        if (
            not isinstance(record, dict)
            or set(record) != {"reason", "originals"}
            or record["reason"] not in ("order_outcome_unknown", "cancel_outcome_unknown")
            or not isinstance(record["originals"], list)
        ):
            raise ValueError("invalid execution state")
        for row in record["originals"]:
            if (
                not isinstance(row, dict)
                or set(row) != {"symbol", "order_id", "client_order_id"}
                or not isinstance(row["symbol"], str)
                or not row["symbol"]
                or (row["order_id"] is None and row["client_order_id"] is None)
                or any(
                    value is not None and (not isinstance(value, str) or not value)
                    for value in (row["order_id"], row["client_order_id"])
                )
            ):
                raise ValueError("invalid execution identity")

    def consume_order_rate(self, count: int) -> None:
        if type(count) is not int or count <= 0:
            raise ValueError("order count must be a positive integer")
        now, clock = self.wall_clock(), self.clock()
        if (
            not math.isfinite(now)
            or now < 0
            or not math.isfinite(clock)
            or (self._last_rate_clock is not None and clock < self._last_rate_clock)
            or (self._order_attempts and now < self._order_attempts[-1])
        ):
            self.halted = "order_rate_clock"
            raise RiskViolation(self.halted)
        self._last_rate_clock = clock
        retained = [
            (wall, mono)
            for wall, mono in zip(self._order_attempts, self._rate_monotonic, strict=True)
            if wall > now - 60 or mono > clock - 60
        ]
        self._order_attempts = [wall for wall, _ in retained]
        self._rate_monotonic = [mono for _, mono in retained]
        if len(self._order_attempts) + count > self.limits.max_orders_per_minute:
            self.halted = "order_rate_limit"
            raise RiskViolation(self.halted)
        # Persist the entire batch before dispatch; rejects and cancellations never refund it.
        self._order_attempts.extend([now] * count)
        self._rate_monotonic.extend([clock] * count)
        self._persist()

    def refresh_order_rate(self, count: int) -> None:
        now, clock = self.wall_clock(), self.clock()
        if (
            not math.isfinite(now)
            or not math.isfinite(clock)
            or now < self._order_attempts[-1]
            or clock < self._rate_monotonic[-1]
        ):
            self.halted = "order_rate_clock"
            raise RiskViolation(self.halted)
        # Disk/network waits cannot make a dispatched batch's budget expire early.
        self._order_attempts[-count:] = [now] * count
        self._rate_monotonic[-count:] = [clock] * count
        self._last_rate_clock = clock

    def begin_execution(
        self, reason: str, originals: Sequence[OrderRequest | CancelRequest]
    ) -> bool:
        if self._execution is not None:
            if reason == "cancel_outcome_unknown":
                retained = list(self._execution["originals"])
                for original in originals:
                    row = {
                        "symbol": original.symbol,
                        "order_id": getattr(original, "order_id", None),
                        "client_order_id": original.client_order_id,
                    }
                    if row not in retained:
                        retained.append(row)
                if retained != self._execution["originals"]:
                    expanded = {**self._execution, "originals": retained}
                    self._validate_execution(expanded)
                    self._execution = expanded
                    self._persist()
            return False
        if reason == "cancel_outcome_unknown" and not originals:
            return False  # A cancel latch naming no original could never be resolved.
        record = {
            "reason": reason,
            "originals": [
                {
                    "symbol": row.symbol,
                    "order_id": getattr(row, "order_id", None),
                    "client_order_id": row.client_order_id,
                }
                for row in originals
            ],
        }
        self._validate_execution(record)
        self._execution = record
        self._persist()
        return True

    def execution_cancellations(self, symbol: str | None) -> tuple[CancelRequest, ...]:
        if self._execution is None:
            return ()
        return tuple(
            CancelRequest(row["symbol"], row["order_id"], row["client_order_id"])
            for row in self._execution["originals"]
            if symbol is None or row["symbol"] == symbol
        )

    def finish_execution(self, owned: bool) -> None:
        if not owned:
            return
        original = self._execution
        self._execution = None
        try:
            self._persist()
        except BaseException:
            self._execution = original
            raise

    def resolve_cancel_latch(
        self, open_orders: Sequence[OpenOrder], history: Sequence[HistoricalOrder]
    ) -> bool:
        """Clear a durable cancel latch only when the venue proves every original terminal."""
        record = self._execution
        if record is None or record["reason"] != "cancel_outcome_unknown":
            return False
        if not venue_proves_closed(self.execution_cancellations(None), open_orders, history):
            return False
        halted, self.halted = self.halted, "daily_loss" if self._loss_halted_at_load else None
        try:
            self.finish_execution(True)
        except BaseException:
            self.halted = halted
            raise
        return True

    def _check_state(self) -> None:
        try:
            if self._state_store is not None:
                self._state_store.check()
        except OSError as error:
            self.halted = "risk_state_unavailable"
            raise RiskViolation(self.halted) from error

    async def flush_state(self) -> None:
        try:
            if self._state_store is not None:
                await self._state_store.flush()
        except OSError as error:
            self.halted = "risk_state_unavailable"
            raise RiskViolation(self.halted) from error

    async def close_state(self) -> None:
        try:
            if self._state_store is not None:
                await self._state_store.close()
        except OSError as error:
            self.halted = "risk_state_unavailable"
            raise RiskViolation(self.halted) from error

    def _persist(self) -> None:
        if self._state_store is None:
            return
        try:
            self._state_store.submit(
                json.dumps(
                    {
                        "day": self._day,
                        "equity_start": str(self._equity_start)
                        if self._equity_start is not None
                        else None,
                        "loss_halted": self.halted == "daily_loss",
                        "execution": self._execution,
                        "order_attempts": self._order_attempts,
                    }
                )
            )
        except OSError as error:
            self.halted = "risk_state_unavailable"
            raise RiskViolation(self.halted) from error

    def update_book(self, book: Book) -> None:
        prior = self.books.get(book.symbol)
        if prior is not None and book.timestamp_ms < prior.timestamp_ms:
            raise RiskViolation("regressed_book")
        # Replaying the same source cut cannot refresh its arrival deadline.
        if prior is not None and book.timestamp_ms == prior.timestamp_ms:
            book = replace(book, received_at=min(book.received_at, prior.received_at))
        self.books[book.symbol] = book

    def _observe_account(self, account: Account) -> None:
        self._check_state()
        if not _positive(account.equity) or any(
            not isinstance(v, Decimal) or not v.is_finite() for v in account.positions.values()
        ):
            raise RiskViolation("invalid_account")
        self.account_state = account
        day = datetime.fromtimestamp(self.wall_clock(), UTC).date().isoformat()
        if self._day != day:
            self._day, self._equity_start = day, account.equity
            if self.halted == "daily_loss":
                self.halted = None
            self._persist()
        if (
            self._equity_start is not None
            and self._equity_start - account.equity >= self.limits.max_daily_loss
        ):
            self.halted = "daily_loss"
            self._persist()
            raise RiskViolation(self.halted)

    def update_account(self, account: Account) -> None:
        self._observe_account(account)
        if self._execution is not None:
            self.halted = self._execution["reason"]
        if self.halted is not None:
            raise RiskViolation(self.halted)

    def revalidate_execution(
        self, orders: Sequence[OrderRequest], account: Account, open_orders: Sequence[OpenOrder]
    ) -> None:
        self._observe_account(account)
        if self.halted is not None:
            raise RiskViolation(self.halted)
        self.set_open_orders(open_orders)
        retained = {
            row.client_order_id: self.pending.pop(row.client_order_id)
            for row in orders
            if row.client_order_id in self.pending
        }
        try:
            for row in orders:
                self.reserve(row)
        except BaseException:
            self.pending.update(retained)
            raise

    def resume_after_disconnect(self) -> bool:
        """Clear a halt caused only by a dropped read stream; unresolved intent stays latched."""
        if self._execution is not None or self.halted not in (None, "pretrade_refused"):
            return False
        self.halted = None
        return True

    def set_open_orders(self, orders: Sequence[OpenOrder | OrderRequest]) -> None:
        self.open_orders = list(orders)

    def has_market_exposure(self, symbol: str) -> bool:
        if self.account_state is None:
            return True
        return self.account_state.positions.get(symbol, Decimal(0)) != 0 or any(
            order.symbol == symbol for order in (*self.pending.values(), *self.open_orders)
        )

    def check_fresh(self, symbol: str) -> Book:
        self._check_state()
        if self.halted is not None:
            raise RiskViolation(self.halted)
        book, account = self.books.get(symbol), self.account_state
        now = self.clock()
        if book is None or not 0 <= now - book.received_at <= self.limits.stale_book_seconds:
            raise StaleMarket(symbol, "stale_book")
        if (
            not book.bids
            or not book.asks
            or not _positive(book.bids[0].price)
            or not _positive(book.asks[0].price)
            or book.bids[0].price >= book.asks[0].price
        ):
            raise RiskViolation("invalid_book")
        source_age = self.wall_clock() - book.timestamp_ms / 1000
        if not -1 <= source_age <= self.limits.stale_book_seconds:
            raise StaleMarket(symbol, "stale_book_source")
        if account is None or not 0 <= now - account.received_at <= self.limits.account_age_seconds:
            raise StaleAccount()
        if self.reference_check is not None:
            self.reference_check(book)
        return book

    def loss_headroom(self) -> Decimal:
        """Fraction of the position cap allowed today: 1 until half of max_daily_loss is used,
        then falling linearly to 0 at the limit, so a losing day can only reduce inventory."""
        if self._equity_start is None or self.account_state is None:
            return Decimal(1)
        loss = self._equity_start - self.account_state.equity
        half = self.limits.max_daily_loss / 2
        return min(Decimal(1), max(Decimal(0), (self.limits.max_daily_loss - loss) / half))

    def validate_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> None:
        book = self.check_fresh(symbol)
        if side not in ("buy", "sell") or not _positive(quantity) or not _positive(price):
            raise RiskViolation("invalid_order")
        reference = max(price, book.asks[0].price)
        if quantity * reference > self.limits.max_order_notional:
            raise RiskViolation("max_order_notional")
        if self.position_limits and symbol not in self.position_limits:
            raise RiskViolation("unconfigured_market")
        assert self.account_state is not None
        position_cap = self.position_limits.get(symbol, self.limits.max_position)
        position_cap *= self.loss_headroom()
        position = self.account_state.positions.get(symbol, Decimal("0"))
        pending_ids = set(self.pending)
        outstanding = list(self.pending.values()) + [
            order for order in self.open_orders if order.client_order_id not in pending_ids
        ]
        buys = quantity if side == "buy" else Decimal("0")
        sells = quantity if side == "sell" else Decimal("0")
        for order in outstanding:
            if order.symbol != symbol:
                continue
            if order.side not in ("buy", "sell") or not _positive(order.quantity):
                raise RiskViolation("invalid_open_order")
            if order.side == "buy":
                buys += order.quantity
            else:
                sells += order.quantity
        # Check only the side this order adds to: an order reducing exposure always fits, even
        # when a shrunken (loss-scaled) cap is already below the current position.
        if (side == "buy" and position + buys > position_cap) or (
            side == "sell" and position - sells < -position_cap
        ):
            raise RiskViolation("max_position")

        if self.limits.max_total_notional is not None:
            self._check_total_notional(outstanding, symbol, side, quantity, price)

    def _check_total_notional(
        self,
        outstanding: Sequence[OpenOrder | OrderRequest],
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> None:
        assert self.account_state is not None and self.limits.max_total_notional is not None
        exposures = {
            key: [Decimal(0), Decimal(0), Decimal(0)]
            for key, value in self.account_state.positions.items()
            if value != 0
        }
        for order_symbol, order_side, qty, limit in [
            (o.symbol, o.side, o.quantity, o.price) for o in outstanding
        ] + [(symbol, side, quantity, price)]:
            if order_side not in ("buy", "sell") or not _positive(qty) or not _positive(limit):
                raise RiskViolation("invalid_open_order")
            row = exposures.setdefault(order_symbol, [Decimal(0), Decimal(0), Decimal(0)])
            row[0 if order_side == "buy" else 1] += qty
            row[2] = max(row[2], limit)
        total = Decimal(0)
        for market, (buys, sells, limit) in exposures.items():
            book = self.check_fresh(market)
            position = self.account_state.positions.get(market, Decimal(0))
            worst = max(abs(position + buys), abs(position - sells))
            total += worst * max(limit, book.asks[0].price)
        if total > self.limits.max_total_notional:
            raise RiskViolation("max_total_notional")

    def reserve(self, order: OrderRequest) -> None:
        if (
            not order.client_order_id
            or order.client_order_id in self.pending
            or any(o.client_order_id == order.client_order_id for o in self.open_orders)
        ):
            raise RiskViolation("duplicate_or_missing_order_id")
        self.validate_order(order.symbol, order.side, order.quantity, order.price)
        self.pending[order.client_order_id] = order


class RiskManagedClient:
    """The sole strategy write boundary; unknown outcomes never trigger a resend."""

    def __init__(
        self,
        client: TradingClient,
        guard: RiskGuard,
        *,
        stop_event: asyncio.Event | None = None,
        cleanup_symbol: str | None = None,
        cleanup_symbols: tuple[str, ...] | None = None,
        metrics: Metrics | None = None,
        isolate_stale_markets: bool = False,
        order_proof: Callable[[Sequence[CancelRequest]], Awaitable[bool]] | None = None,
        proof_window_seconds: float = 0.0,
        engine_halted: Callable[[BaseException | None], Awaitable[bool]] | None = None,
        halt_window_seconds: float = 0.0,
    ) -> None:
        self.isolate_stale_markets = isolate_stale_markets
        # Venue read proving cancels terminal when a receipt is missing or late.
        self.order_proof = order_proof
        self.proof_window_seconds = proof_window_seconds
        # True while Glyde reports its engine halted; that wait has its own, longer budget.
        self.engine_halted = engine_halted
        self.halt_window_seconds = halt_window_seconds
        self.client, self.guard = client, guard
        self.metrics = metrics
        self._lock = asyncio.Lock()
        self._order_ids: dict[str, str] = {}
        self._closed = False
        self._stop_event = stop_event
        self._cleanup_symbols = cleanup_symbols or (cleanup_symbol,)

    def _ensure_running(self) -> None:
        self.guard._check_state()
        if self._closed or (self._stop_event is not None and self._stop_event.is_set()):
            raise RiskViolation("client_closed")

    def _owned(self, order: OpenOrder | OrderRequest) -> bool:
        """Bot-tagged or placed by this client; other account orders are never cancelled."""
        return (
            is_bot_order(order.client_order_id)
            or getattr(order, "order_id", None) in self._order_ids
            or order.client_order_id in self.guard.pending
            or order.client_order_id in self._order_ids.values()
        )

    async def _cancel_before_discovery(
        self, symbol: str | None = None, *, deferrable: bool = True
    ) -> None:
        ids = {key: oid for oid, key in self._order_ids.items()}
        requests = []
        seen = set()
        for order in [*self.guard.pending.values(), *self.guard.open_orders]:
            if (symbol is not None and order.symbol != symbol) or not self._owned(order):
                continue
            oid = getattr(order, "order_id", None) or ids.get(order.client_order_id or "")
            request = CancelRequest(
                order.symbol,
                order_id=oid,
                client_order_id=None if oid is not None else order.client_order_id,
            )
            identity = request.symbol, request.order_id, request.client_order_id
            if identity not in seen:
                requests.append(request)
                seen.add(identity)
        # Restarts retain durable originals even when in-memory reservations are gone.
        for original in self.guard.execution_cancellations(symbol):
            oid = original.order_id or ids.get(original.client_order_id or "")
            request = CancelRequest(
                original.symbol,
                order_id=oid,
                client_order_id=None if oid is not None else original.client_order_id,
            )
            identity = request.symbol, request.order_id, request.client_order_id
            if identity not in seen:
                requests.append(request)
                seen.add(identity)
        failure: Exception | None = None
        owned = False
        try:
            owned = self.guard.begin_execution("cancel_outcome_unknown", requests)
            await self.guard.flush_state()
        except Exception as error:
            failure = error
        unproven: list[CancelRequest] = []
        unproven_error: Exception | None = None
        for request in requests:
            try:
                results = await self.client.cancel_orders([request])
                if len(results) != 1:
                    raise RiskViolation("cancel_outcome_unknown")
                result = results[0]
                if result.status not in ("cancelled", "already_closed"):
                    raise RiskViolation("cancel_outcome_unknown")
            except Exception as error:
                unproven.append(request)
                unproven_error = unproven_error or error
                continue
            if getattr(result, "order_id", None) not in (None, request.order_id) or getattr(
                result, "client_order_id", None
            ) not in (
                None,
                request.client_order_id or self._order_ids.get(request.order_id or ""),
            ):
                failure = failure or RiskViolation("cancel_identity_mismatch")
            elif self.metrics and result.status == "cancelled":
                self.metrics.count("quotes_cancelled")
        if unproven and not await self._venue_proves_closed(unproven, unproven_error):
            failure = failure or unproven_error
        try:
            await self.client.cancel_all(symbol)
        except Exception as error:
            if deferrable and failure is None and reconnectable(error):
                # A transient read failure leaves no unknown original; the next connection's
                # reconcile repeats the sweep before any quote.
                self.guard.finish_execution(owned)
                self._retire_owned(symbol)
                raise VenueSweepDeferred("venue sweep awaits a fresh snapshot") from error
            failure = failure or error
        if failure is not None:
            raise RiskViolation("cancel_outcome_unknown") from failure

        self.guard.finish_execution(owned)
        self._retire_owned(symbol)

    async def _cancel_one(self, request: CancelRequest) -> bool:
        try:
            results = await self.client.cancel_orders([request])
        except Exception:
            return False
        return len(results) == 1 and results[0].status in ("cancelled", "already_closed")

    async def _engine_is_halted(self, error: BaseException | None) -> bool:
        if self.engine_halted is None:
            return False
        try:
            return await self.engine_halted(error)
        except Exception:
            return False

    async def _venue_proves_closed(
        self, requests: Sequence[CancelRequest], cause: BaseException | None = None
    ) -> bool:
        """Retry venue proof with backoff, while trading stays paused under the lock.

        Time Glyde reports its engine halted never counts against the proof window; it is
        bounded by the halt window instead. Cancels the halt refused are re-sent once it ends.
        """
        if self.order_proof is None or not requests:
            return False
        started = time.monotonic()
        deadline = started + self.proof_window_seconds
        halt_deadline = started + self.halt_window_seconds
        delay = PROOF_RETRY_INITIAL_SECONDS
        unproven, was_halted = list(requests), engine_unavailable(cause)
        while True:
            failure: BaseException | None = None
            try:
                if await self.order_proof(unproven):
                    return True
            except Exception as error:  # Unavailable proof is retried, then latched.
                failure = error
            # A halting or closing sweep checks once; only live trading waits out the window.
            stopping = (
                self._closed
                or self.guard.halted is not None
                or (self._stop_event is not None and self._stop_event.is_set())
            )
            if stopping:
                return False
            halted = await self._engine_is_halted(failure)
            if was_halted and not halted:
                # The engine is back: a cancel the halt refused is sent again, then proven.
                unproven = [row for row in unproven if not await self._cancel_one(row)]
                if not unproven:
                    return True
                was_halted = False
                continue
            was_halted = was_halted or halted
            now = time.monotonic()
            if halted:
                deadline = max(deadline, now + self.proof_window_seconds)
                remaining = halt_deadline - now
            else:
                remaining = deadline - now
            if remaining <= 0:
                return False
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, PROOF_RETRY_MAX_SECONDS)

    def _retire_owned(self, symbol: str | None) -> None:
        # Foreign orders stay counted as exposure; only the bot's own are retired.
        self.guard.open_orders = [
            order
            for order in self.guard.open_orders
            if (symbol is not None and order.symbol != symbol) or not self._owned(order)
        ]
        self.guard.pending = {
            key: order
            for key, order in self.guard.pending.items()
            if symbol is not None and order.symbol != symbol
        }

    async def _cancel_and_halt(self, reason: str, *, deferrable: bool = True) -> None:
        self.guard.halted = self.guard.halted or reason
        # Reservations clear only after known cancels and discovery both succeed.
        failure = None
        for symbol in self._cleanup_symbols:
            try:
                await self._cancel_before_discovery(symbol, deferrable=deferrable)
            except Exception as error:
                failure = failure or error
        if failure is not None:
            raise failure

    async def _refuse_undispatched(self, error: BaseException, count: int) -> None:
        """Clean up after refusing orders that were never sent; the caller re-raises error."""
        reason = (
            str(error) if isinstance(error, (RiskViolation, StaleAccount)) else "pretrade_refused"
        )
        if self.metrics:
            self.metrics.refused(reason, count)
        if isinstance(error, StaleAccount):
            return  # Nothing was sent; the runner treats it as a drop and retires quotes.
        if (
            self.isolate_stale_markets
            and isinstance(error, StaleMarket)
            and error.symbol in self.guard.position_limits
        ):
            try:
                await self._cancel_before_discovery(error.symbol)
            except VenueSweepDeferred:
                raise
            except BaseException:
                await self._cancel_and_halt("cancel_outcome_unknown")
                raise
            return
        try:
            await self._cancel_and_halt(self.guard.halted or reason)
        except Exception as cleanup_error:
            raise error from cleanup_error

    async def place_orders(self, orders: Sequence[OrderRequest]) -> list[OrderResult]:
        async with self._lock:
            self._ensure_running()
            if not orders:
                return []
            reserved: list[str] = []
            prepared = [
                replace(order, client_order_id=order.client_order_id or bot_client_order_id())
                for order in orders
            ]
            try:
                self.guard.update_account(await self.client.account())
                self.guard.set_open_orders(await self.client.open_orders())
                self._ensure_running()
                over_cap: set[str] = set()
                for order in prepared:
                    assert order.client_order_id is not None
                    try:
                        self.guard.reserve(order)
                    except RiskViolation as error:
                        # Over the position cap: drop this order (the side adding inventory)
                        # and keep quoting the rest instead of stopping the run.
                        if str(error) != "max_position":
                            raise
                        over_cap.add(order.client_order_id)
                        continue
                    reserved.append(order.client_order_id)
                if over_cap and self.metrics:
                    self.metrics.refused("max_position", len(over_cap))
                skipped = [
                    OrderResult(
                        "rejected", client_order_id=order.client_order_id, error="max_position"
                    )
                    for order in prepared
                    if order.client_order_id in over_cap
                ]
                requested = [order.client_order_id for order in prepared]
                prepared = [order for order in prepared if order.client_order_id not in over_cap]
                if not prepared:
                    return skipped
                self.guard.consume_order_rate(len(prepared))
            except BaseException as error:
                for key in reserved:
                    self.guard.pending.pop(key, None)
                await self._refuse_undispatched(error, len(prepared))
                raise
            refused = dispatched = owned = False
            try:
                owned = self.guard.begin_execution("order_outcome_unknown", prepared)
                await self.guard.flush_state()
                self._ensure_running()
                account = await self.client.account()
                open_orders = await self.client.open_orders()
                self._ensure_running()
                previous_day = self.guard._day
                self.guard.revalidate_execution(prepared, account, open_orders)
                if self.guard._day != previous_day:
                    await self.guard.flush_state()
                    for order in prepared:
                        self.guard.check_fresh(order.symbol)
                self.guard.refresh_order_rate(len(prepared))
                dispatched = True
                try:
                    results = await self.client.place_orders(prepared)
                except RateLimited as error:
                    # Refused before processing: nothing was placed, so nothing is unknown.
                    for order in prepared:
                        self.guard.pending.pop(order.client_order_id or "", None)
                    self.guard.finish_execution(owned)
                    refused = True
                    self._rate_limited(error, len(prepared))
                    raise
                except ActionRefused:
                    # Glyde refused the whole action: nothing was placed, so there is no
                    # unknown outcome to reconcile and none of these orders to cancel.
                    for order in prepared:
                        self.guard.pending.pop(order.client_order_id or "", None)
                    self.guard.finish_execution(owned)
                    refused = True
                    if self.metrics:
                        self.metrics.refused("venue_refused", len(prepared))
                    raise
                self.guard.refresh_order_rate(len(prepared))
                if len(results) != len(prepared) or any(
                    r.status not in ("resting", "filled", "rejected") for r in results
                ):
                    raise RiskViolation("order_outcome_unknown")
                for order, result in zip(prepared, results, strict=True):
                    assert order.client_order_id is not None
                    key = order.client_order_id
                    if result.client_order_id not in (None, key):
                        raise RiskViolation("order_identity_mismatch")
                    if result.status == "resting":
                        if not result.order_id:
                            raise RiskViolation("order_outcome_unknown")
                        self._order_ids[result.order_id] = key
                    else:
                        self.guard.pending.pop(key, None)
                        if result.status == "filled":
                            self.guard.account_state = None
                self.guard.finish_execution(owned)
                if self.metrics:
                    self.metrics.count(
                        "quotes_placed", sum(r.status in {"resting", "filled"} for r in results)
                    )
                    self.metrics.refused(
                        "venue_rejected", sum(r.status == "rejected" for r in results)
                    )
                if not skipped:
                    return results
                # Report in the caller's order, the dropped over-cap orders as rejected.
                by_key = {row.client_order_id: row for row in skipped}
                by_key.update(
                    (order.client_order_id, row)
                    for order, row in zip(prepared, results, strict=True)
                )
                return [by_key[key] for key in requested]
            except BaseException as error:
                if not dispatched:
                    # Nothing reached Glyde, so no outcome is unknown: e.g. a peer book expiring
                    # during revalidation. Release the intent and refuse as pretrade does.
                    for order in prepared:
                        self.guard.pending.pop(order.client_order_id or "", None)
                    try:
                        self.guard.finish_execution(owned)
                    except BaseException:
                        await self._cancel_and_halt("order_outcome_unknown")
                        raise
                    await self._refuse_undispatched(error, len(prepared))
                    raise
                if refused and isinstance(error, RateLimited):
                    # Nothing was placed and nothing is unknown: the strategy backs off.
                    raise
                if refused and isinstance(error, EngineUnavailable):
                    # Retire quotes and wait the halt out exactly as a stream drop does.
                    raise StreamDisconnected("exchange refused orders: engine unavailable") from (
                        error
                    )
                reason = "order_refused" if refused else "order_outcome_unknown"
                try:
                    await self._cancel_and_halt(reason)
                except Exception as cleanup_error:
                    raise error from cleanup_error
                if refused:
                    raise RiskViolation(reason) from error
                raise

    async def cancel_orders(self, orders: Sequence[CancelRequest]) -> list[CancelResult]:
        async with self._lock:
            try:
                owned = self.guard.begin_execution("cancel_outcome_unknown", orders)
                await self.guard.flush_state()
                try:
                    results = await self.client.cancel_orders(orders)
                    if len(results) != len(orders) or any(
                        r.status not in ("cancelled", "already_closed") for r in results
                    ):
                        raise RiskViolation("cancel_outcome_unknown")
                except RateLimited as error:
                    # Refused before processing: every order still rests, nothing is unknown.
                    self.guard.finish_execution(owned)
                    self._rate_limited(error, len(orders))
                    raise
                except Exception as error:
                    # A late or missing receipt is not an unknown outcome once the venue
                    # shows every order terminal. Retry each first: a batch failure can
                    # leave later cancels unsent, and receipted ones answer locally.
                    unproven = [row for row in orders if not await self._cancel_one(row)]
                    if unproven and not await self._venue_proves_closed(unproven, error):
                        raise
                    results = [
                        CancelResult("already_closed", row.order_id, row.client_order_id)
                        for row in orders
                    ]
                for request, result in zip(orders, results, strict=True):
                    if getattr(result, "order_id", None) not in (None, request.order_id) or getattr(
                        result, "client_order_id", None
                    ) not in (None, request.client_order_id):
                        raise RiskViolation("cancel_identity_mismatch")
                    key = request.client_order_id or self._order_ids.get(request.order_id or "")
                    if key is not None:
                        self.guard.pending.pop(key, None)
                self.guard.finish_execution(owned)
                if self.metrics:
                    self.metrics.count(
                        "quotes_cancelled", sum(r.status == "cancelled" for r in results)
                    )
                return results
            except RateLimited:
                raise
            except BaseException:
                await self._cancel_and_halt("cancel_outcome_unknown")
                raise

    def _rate_limited(self, error: RateLimited, count: int) -> None:
        """A rate refusal is a visible back-off, not a fault: say how long to wait."""
        _LOG.warning("%s (%d orders not sent)", error, count)
        if self.metrics:
            self.metrics.refused("rate_limited", count)

    async def cancel_all(self, symbol: str | None = None) -> None:
        async with self._lock:
            try:
                await self._cancel_before_discovery(symbol)
            except VenueSweepDeferred:
                raise
            except BaseException:
                self.guard.halted = self.guard.halted or "cancel_outcome_unknown"
                raise

    async def account(self) -> Account:
        self._ensure_running()
        account = await self.client.account()
        self._ensure_running()
        return account

    async def open_orders(self) -> list[OpenOrder]:
        return await self.client.open_orders()

    async def observe_account(self) -> None:
        """Check the account between boundary actions; an in-flight intent is not an outcome."""
        async with self._lock:
            self.guard.update_account(await self.client.account())

    async def refresh_account(self) -> None:
        async with self._lock:
            self._ensure_running()
            account = await self.client.account()
            orders = await self.client.open_orders()
            self._ensure_running()
            self.guard.update_account(account)
            self.guard.set_open_orders(orders)

    async def interrupt(self, tasks: Sequence[asyncio.Task[Any]]) -> list[BaseException | None]:
        """Cancel tasks between boundary actions, never during a dispatched write."""
        async with self._lock:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        return [result if isinstance(result, BaseException) else None for result in results]

    async def on_disconnect(self) -> None:
        async with self._lock:
            await self._cancel_and_halt("disconnected")

    async def check_liveness(self) -> None:
        async with self._lock:
            try:
                stale = set()
                for order in self.guard.pending.values():
                    try:
                        self.guard.check_fresh(order.symbol)
                    except StaleMarket as error:
                        if not self.isolate_stale_markets:
                            raise
                        stale.add(error.symbol)
                for symbol in stale:
                    await self._cancel_before_discovery(symbol)
            except RiskViolation:
                await self._cancel_and_halt("stale_inputs")
                raise

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            try:
                # No next connection repeats a sweep after close, so an unread venue at
                # shutdown is an unknown outcome, never a deferral.
                await self._cancel_and_halt("closed", deferrable=False)
            finally:
                await self.guard.close_state()

    async def __aenter__(self) -> RiskManagedClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
