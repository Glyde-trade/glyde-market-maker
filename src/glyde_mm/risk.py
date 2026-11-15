"""Local pre-trade limits. The venue remains the final authority for every order."""

from __future__ import annotations

import asyncio
import json
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

if TYPE_CHECKING:
    from .models import (
        Account,
        Book,
        CancelRequest,
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

    def __post_init__(self) -> None:
        for name in ("max_position", "max_order_notional", "max_daily_loss"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
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
    ) -> None:
        self.limits = limits
        self.clock = clock
        self.wall_clock = wall_clock
        self.state_path = state_path
        self.books: dict[str, Book] = {}
        self.account_state: Account | None = None
        self.pending: dict[str, OrderRequest] = {}
        self.open_orders: list[OpenOrder | OrderRequest] = []
        self.halted: str | None = None
        self._day: str | None = None
        self._equity_start: Decimal | None = None
        if state_path is not None and state_path.exists():
            try:
                data = json.loads(state_path.read_text())
                self._day = datetime.strptime(data["day"], "%Y-%m-%d").date().isoformat()
                self._equity_start = Decimal(data["equity_start"])
                if not _positive(self._equity_start) or not isinstance(data["loss_halted"], bool):
                    raise ValueError("invalid risk state")
                if data["loss_halted"]:
                    self.halted = "daily_loss"
            except (ValueError, KeyError, TypeError, OSError) as exc:
                raise RiskViolation("risk_state_unavailable") from exc

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
                        "equity_start": str(self._equity_start),
                        "loss_halted": self.halted == "daily_loss",
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
        return book

    def validate_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> None:
        book = self.check_fresh(symbol)
        if side not in ("buy", "sell") or not _positive(quantity) or not _positive(price):
            raise RiskViolation("invalid_order")
        reference = max(price, book.asks[0].price)
        if quantity * reference > self.limits.max_order_notional:
            raise RiskViolation("max_order_notional")
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
        if (
            position + buys > self.limits.max_position
            or position - sells < -self.limits.max_position
        ):
            raise RiskViolation("max_position")

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

    def __init__(self, client: TradingClient, guard: RiskGuard) -> None:
        self.client, self.guard = client, guard
        self._lock = asyncio.Lock()
        self._order_ids: dict[str, str] = {}
        self._closed = False

    async def _cancel_and_halt(self, reason: str) -> None:
        self.guard.halted = self.guard.halted or reason
        # Do not erase reservations: cancellation may itself be ambiguous.
        await self.client.cancel_all()

    async def place_orders(self, orders: Sequence[OrderRequest]) -> list[OrderResult]:
        async with self._lock:
            if self._closed:
                raise RiskViolation("client_closed")
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
                for order in prepared:
                    self.guard.reserve(order)
                    reserved.append(order.client_order_id)
            except BaseException:
                for key in reserved:
                    self.guard.pending.pop(key, None)
                await self._cancel_and_halt(self.guard.halted or "pretrade_refused")
                raise
            try:
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
                return results
            except BaseException:
                await self._cancel_and_halt("order_outcome_unknown")
                raise

    async def cancel_orders(self, orders: Sequence[CancelRequest]) -> list[CancelResult]:
        async with self._lock:
            try:
                results = await self.client.cancel_orders(orders)
                if len(results) != len(orders) or any(r.status != "cancelled" for r in results):
                    raise RiskViolation("cancel_outcome_unknown")
                for request, result in zip(orders, results, strict=True):
                    if getattr(result, "order_id", None) not in (None, request.order_id) or getattr(
                        result, "client_order_id", None
                    ) not in (None, request.client_order_id):
                        raise RiskViolation("cancel_identity_mismatch")
                    key = request.client_order_id or self._order_ids.get(request.order_id or "")
                    if key is not None:
                        self.guard.pending.pop(key, None)
                return results
            except BaseException:
                await self._cancel_and_halt("cancel_outcome_unknown")
                raise

    async def cancel_all(self, symbol: str | None = None) -> None:
        async with self._lock:
            try:
                await self.client.cancel_all(symbol)
            except BaseException:
                self.guard.halted = self.guard.halted or "cancel_outcome_unknown"
                raise
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

    async def account(self) -> Account:
        return await self.client.account()

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
