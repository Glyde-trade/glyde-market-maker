"""Local pre-trade limits. The venue remains the final authority for every order."""

from __future__ import annotations

import asyncio
import json
import math
import os
import secrets
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from .models import CancelRequest

if TYPE_CHECKING:
    from .models import (
        Account,
        Book,
        CancelResult,
        OpenOrder,
        OrderRequest,
        OrderResult,
        TradingClient,
    )


class RiskViolation(RuntimeError):
    """A quote was refused before dispatch; the message contains no credentials."""


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
        self._execution: dict | None = None
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
                self._order_attempts = data.get("order_attempts", [])
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
                wall, clock = self.wall_clock(), self.clock()
                self._rate_monotonic = [
                    clock - min(60, wall - stamp) for stamp in self._order_attempts
                ]
                self._execution = data.get("execution")
                if self._execution is not None:
                    self._validate_execution(self._execution)
                    self.halted = self._execution["reason"]
                elif data["loss_halted"]:
                    self.halted = "daily_loss"
            except (ValueError, KeyError, TypeError, OSError) as exc:
                raise RiskViolation("risk_state_unavailable") from exc

    @staticmethod
    def _validate_execution(record: dict) -> None:
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

    def begin_execution(self, reason: str, originals: Sequence) -> bool:
        if self._execution is not None:
            return False
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

    def _persist(self) -> None:
        if self.state_path is None:
            return
        path = self.state_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
                temporary = handle.name
                json.dump(
                    {
                        "day": self._day,
                        "equity_start": str(self._equity_start)
                        if self._equity_start is not None
                        else None,
                        "loss_halted": self.halted == "daily_loss",
                        "execution": self._execution,
                        "order_attempts": self._order_attempts,
                    },
                    handle,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            self.halted = "risk_state_unavailable"
            raise RiskViolation(self.halted) from exc
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def update_book(self, book: Book) -> None:
        prior = self.books.get(book.symbol)
        if prior is not None and book.timestamp_ms < prior.timestamp_ms:
            raise RiskViolation("regressed_book")
        # Replaying the same source cut cannot refresh its arrival deadline.
        if prior is not None and book.timestamp_ms == prior.timestamp_ms:
            book = replace(book, received_at=min(book.received_at, prior.received_at))
        self.books[book.symbol] = book

    def update_account(self, account: Account) -> None:
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
        if self._execution is not None:
            self.halted = self._execution["reason"]
        if self.halted is not None:
            raise RiskViolation(self.halted)

    def set_open_orders(self, orders: Sequence[OpenOrder | OrderRequest]) -> None:
        self.open_orders = list(orders)

    def check_fresh(self, symbol: str) -> Book:
        if self.halted is not None:
            raise RiskViolation(self.halted)
        book, account = self.books.get(symbol), self.account_state
        now = self.clock()
        if book is None or not 0 <= now - book.received_at <= self.limits.stale_book_seconds:
            raise RiskViolation("stale_book")
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
            raise RiskViolation("stale_book_source")
        if account is None or not 0 <= now - account.received_at <= self.limits.account_age_seconds:
            raise RiskViolation("stale_account")
        if self.reference_check is not None:
            self.reference_check(book)
        return book

    def validate_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> None:
        book = self.check_fresh(symbol)
        if side not in ("buy", "sell") or not _positive(quantity) or not _positive(price):
            raise RiskViolation("invalid_order")
        reference = max(price, book.asks[0].price)
        if quantity * reference > self.limits.max_order_notional:
            raise RiskViolation("max_order_notional")
        if self.position_limits and symbol not in self.position_limits:
            raise RiskViolation("unconfigured_market")
        position_cap = self.position_limits.get(symbol, self.limits.max_position)
        position = self.account_state.positions.get(symbol, Decimal("0"))  # type: ignore[union-attr]
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
        if position + buys > position_cap or position - sells < -position_cap:
            raise RiskViolation("max_position")

        if self.limits.max_total_notional is not None:
            self._check_total_notional(outstanding, symbol, side, quantity, price)

    def _check_total_notional(self, outstanding, symbol, side, quantity, price) -> None:
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
    ) -> None:
        self.client, self.guard = client, guard
        self._lock = asyncio.Lock()
        self._order_ids: dict[str, str] = {}
        self._closed = False
        self._stop_event = stop_event
        self._cleanup_symbols = cleanup_symbols or (cleanup_symbol,)

    def _ensure_running(self) -> None:
        if self._closed or (self._stop_event is not None and self._stop_event.is_set()):
            raise RiskViolation("client_closed")

    async def _cancel_before_discovery(self, symbol: str | None = None) -> None:
        ids = {key: oid for oid, key in self._order_ids.items()}
        requests = [
            CancelRequest(
                order.symbol, order_id=ids.get(key), client_order_id=None if key in ids else key
            )
            for key, order in self.guard.pending.items()
            if symbol is None or order.symbol == symbol
        ]
        failure: Exception | None = None
        owned = False
        try:
            owned = self.guard.begin_execution("cancel_outcome_unknown", requests)
        except Exception as error:
            failure = error
        for request in requests:
            try:
                results = await self.client.cancel_orders([request])
                if len(results) != 1:
                    raise RiskViolation("cancel_outcome_unknown")
                result = results[0]
                if result.status not in ("cancelled", "already_closed"):
                    raise RiskViolation("cancel_outcome_unknown")
                if getattr(result, "order_id", None) not in (None, request.order_id) or getattr(
                    result, "client_order_id", None
                ) not in (
                    None,
                    request.client_order_id or self._order_ids.get(request.order_id or ""),
                ):
                    raise RiskViolation("cancel_identity_mismatch")
            except Exception as error:
                failure = failure or error
        try:
            await self.client.cancel_all(symbol)
        except Exception as error:
            failure = failure or error
        if failure is not None:
            raise RiskViolation("cancel_outcome_unknown") from failure

        self.guard.finish_execution(owned)
        self.guard.pending = {
            key: order
            for key, order in self.guard.pending.items()
            if symbol is not None and order.symbol != symbol
        }
        self.guard.open_orders = [
            order
            for order in self.guard.open_orders
            if symbol is not None and order.symbol != symbol
        ]

    async def _cancel_and_halt(self, reason: str) -> None:
        self.guard.halted = self.guard.halted or reason
        # Reservations clear only after known cancels and discovery both succeed.
        failure = None
        for symbol in self._cleanup_symbols:
            try:
                await self._cancel_before_discovery(symbol)
            except Exception as error:
                failure = failure or error
        if failure is not None:
            raise failure

    async def place_orders(self, orders: Sequence[OrderRequest]) -> list[OrderResult]:
        async with self._lock:
            self._ensure_running()
            if not orders:
                return []
            reserved: list[str] = []
            prepared = [
                replace(
                    order, client_order_id=order.client_order_id or "0x" + secrets.token_hex(16)
                )
                for order in orders
            ]
            try:
                self.guard.update_account(await self.client.account())
                self.guard.set_open_orders(await self.client.open_orders())
                self._ensure_running()
                for order in prepared:
                    self.guard.reserve(order)
                    reserved.append(order.client_order_id)
                self.guard.consume_order_rate(len(prepared))
            except BaseException as error:
                for key in reserved:
                    self.guard.pending.pop(key, None)
                reason = str(error) if isinstance(error, RiskViolation) else "pretrade_refused"
                await self._cancel_and_halt(self.guard.halted or reason)
                raise
            try:
                owned = self.guard.begin_execution("order_outcome_unknown", prepared)
                results = await self.client.place_orders(prepared)
                if len(results) != len(prepared) or any(
                    r.status not in ("resting", "filled", "rejected") for r in results
                ):
                    raise RiskViolation("order_outcome_unknown")
                for order, result in zip(prepared, results, strict=True):
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
                return results
            except BaseException:
                await self._cancel_and_halt("order_outcome_unknown")
                raise

    async def cancel_orders(self, orders: Sequence[CancelRequest]) -> list[CancelResult]:
        async with self._lock:
            try:
                owned = self.guard.begin_execution("cancel_outcome_unknown", orders)
                results = await self.client.cancel_orders(orders)
                if len(results) != len(orders) or any(
                    r.status not in ("cancelled", "already_closed") for r in results
                ):
                    raise RiskViolation("cancel_outcome_unknown")
                for request, result in zip(orders, results, strict=True):
                    if getattr(result, "order_id", None) not in (None, request.order_id) or getattr(
                        result, "client_order_id", None
                    ) not in (None, request.client_order_id):
                        raise RiskViolation("cancel_identity_mismatch")
                    key = request.client_order_id or self._order_ids.get(request.order_id or "")
                    if key is not None:
                        self.guard.pending.pop(key, None)
                self.guard.finish_execution(owned)
                return results
            except BaseException:
                await self._cancel_and_halt("cancel_outcome_unknown")
                raise

    async def cancel_all(self, symbol: str | None = None) -> None:
        async with self._lock:
            try:
                await self._cancel_before_discovery(symbol)
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

    async def on_disconnect(self) -> None:
        async with self._lock:
            await self._cancel_and_halt("disconnected")

    async def check_liveness(self) -> None:
        async with self._lock:
            try:
                for order in self.guard.pending.values():
                    self.guard.check_fresh(order.symbol)
            except RiskViolation:
                await self._cancel_and_halt("stale_inputs")
                raise

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            await self._cancel_and_halt("closed")

    async def __aenter__(self) -> RiskManagedClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
