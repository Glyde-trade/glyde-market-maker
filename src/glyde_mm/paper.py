"""Deterministic local perp simulator; never holds a signing key or sends orders."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from time import time_ns

from .client import wire_decimal
from .models import (
    Account,
    Book,
    CancelRequest,
    CancelResult,
    Fill,
    OpenOrder,
    OrderRequest,
    OrderResult,
    Trade,
)


class PaperExchange:
    """Local fills from books, or opt-in public tape with a displayed-depth queue.

    Tape mode is used by streaming paper sessions; book-only replay retains its
    existing depth-cross model. Neither model forecasts real execution.
    """

    def __init__(
        self, balance: Decimal = Decimal("10000"), *, clock_ms: Callable[[], int] | None = None
    ) -> None:
        if not balance.is_finite() or balance <= 0:
            raise ValueError("paper balance must be finite and positive")
        self._cash = balance
        self._books: dict[str, Book] = {}
        self._positions: dict[str, Decimal] = {}
        self._entries: dict[str, Decimal] = {}
        self._orders: dict[str, OrderRequest] = {}
        self._terminal: OrderedDict[str, OrderRequest] = OrderedDict()
        self._next_id = 0
        self.fills: list[Fill] = []
        self._clock_ms = clock_ms or (lambda: time_ns() // 1_000_000)
        self._trade_tape = False
        self._trade_after: dict[str, int] = {}
        self._queue_ahead: dict[tuple[str, str, Decimal], Decimal] = {}
        self._trade_cut: dict[str, int] = {}
        self._trade_ids: dict[str, set[str]] = {}

    def use_trade_tape(self) -> None:
        """Select tape fills before placing orders; never mix book and tape budgets."""
        if self._orders or self.fills:
            raise ValueError("select paper fill model before trading")
        self._trade_tape = True

    async def on_book(self, book: Book) -> None:
        old = self._books.get(book.symbol)
        if old is not None and book.timestamp_ms < old.timestamp_ms:
            raise ValueError("paper book timestamp regressed")
        if old is not None and book.timestamp_ms == old.timestamp_ms:
            return
        self._books[book.symbol] = book
        if self._trade_tape:
            return
        # One observed depth budget is shared across all local orders.
        bids = [[level.price, level.quantity] for level in book.bids]
        asks = [[level.price, level.quantity] for level in book.asks]
        for oid, request in list(self._orders.items()):
            if request.symbol != book.symbol:
                continue
            remaining = self._match(oid, request, asks if request.side == "buy" else bids)
            if remaining == 0:
                self._remember_terminal(oid, request)
                del self._orders[oid]
            else:
                self._orders[oid] = replace(request, quantity=remaining)

    def _rest(self, oid: str, order: OrderRequest) -> None:
        self._orders[oid] = order
        if not self._trade_tape:
            return
        book = self._books[order.symbol]
        self._trade_after[oid] = max(
            self._clock_ms(), book.timestamp_ms, self._trade_cut.get(order.symbol, 0)
        )
        key = (order.symbol, order.side, order.price)
        if key not in self._queue_ahead:
            levels = book.bids if order.side == "buy" else book.asks
            self._queue_ahead[key] = sum(
                (row.quantity for row in levels if row.price == order.price), Decimal(0)
            )

    def _remove(self, oid: str) -> OrderRequest:
        order = self._orders.pop(oid)
        self._trade_after.pop(oid, None)
        key = (order.symbol, order.side, order.price)
        if not any((row.symbol, row.side, row.price) == key for row in self._orders.values()):
            self._queue_ahead.pop(key, None)
        return order

    async def on_trade(self, trade: Trade) -> None:
        if not self._trade_tape:
            raise ValueError("paper trade tape is not enabled")
        previous = self._trade_cut.get(trade.symbol, -1)
        if trade.timestamp_ms < previous:
            return
        if trade.timestamp_ms > previous:
            self._trade_ids[trade.symbol] = set()
            self._trade_cut[trade.symbol] = trade.timestamp_ms
        seen = self._trade_ids[trade.symbol]
        if trade.trade_id in seen:
            return
        if len(seen) >= 4096:
            raise ValueError("paper trade identity budget exceeded at one timestamp")
        seen.add(trade.trade_id)
        side = "buy" if trade.side == "sell" else "sell"
        candidates = sorted(
            (
                (oid, row)
                for oid, row in self._orders.items()
                if row.symbol == trade.symbol and row.side == side
            ),
            key=lambda pair: -pair[1].price if side == "buy" else pair[1].price,
        )
        budget = trade.quantity
        for oid, order in candidates:
            if budget <= 0:
                break
            if trade.timestamp_ms <= self._trade_after[oid]:
                continue
            if (side == "buy" and trade.price > order.price) or (
                side == "sell" and trade.price < order.price
            ):
                continue
            key = (order.symbol, side, order.price)
            ahead = self._queue_ahead[key] if trade.price == order.price else Decimal(0)
            consumed = min(budget, ahead)
            budget -= consumed
            self._queue_ahead[key] = ahead - consumed
            quantity = min(budget, order.quantity)
            if order.reduce_only:
                position = self._positions.get(order.symbol, Decimal(0))
                quantity = min(quantity, max(Decimal(0), -position if side == "buy" else position))
            if quantity <= 0:
                continue
            self._fill(oid, order, quantity, order.price, f"{trade.trade_id}:{oid}")
            budget -= quantity
            remaining = order.quantity - quantity
            if remaining:
                self._orders[oid] = replace(order, quantity=remaining)
            else:
                self._remember_terminal(oid, self._remove(oid))

    def _remember_terminal(self, oid: str, request: OrderRequest) -> None:
        self._terminal[oid] = request
        while len(self._terminal) > 4096:
            self._terminal.popitem(last=False)

    def _match(self, oid: str, order: OrderRequest, levels: list[list[Decimal]]) -> Decimal:
        remaining = order.quantity
        for level in levels:
            price, available = level
            if (order.side == "buy" and price > order.price) or (
                order.side == "sell" and price < order.price
            ):
                break
            quantity = min(remaining, available)
            position = self._positions.get(order.symbol, Decimal(0))
            if order.reduce_only:
                if (order.side == "buy" and position >= 0) or (
                    order.side == "sell" and position <= 0
                ):
                    break
                quantity = min(quantity, abs(position))
            if quantity > 0:
                self._fill(oid, order, quantity, price)
                level[1] -= quantity
                remaining -= quantity
            if remaining == 0:
                break
        return remaining

    def _fill(
        self,
        oid: str,
        order: OrderRequest,
        quantity: Decimal,
        price: Decimal,
        trade_id: str | None = None,
    ) -> None:
        previous = self._positions.get(order.symbol, Decimal(0))
        entry = self._entries.get(order.symbol, price)
        delta = quantity if order.side == "buy" else -quantity
        updated = previous + delta
        if previous and previous * delta < 0:
            closed = min(abs(previous), quantity)
            self._cash += closed * (price - entry) * (1 if previous > 0 else -1)
        if previous == 0 or previous * delta > 0:
            self._entries[order.symbol] = (abs(previous) * entry + quantity * price) / abs(updated)
        elif previous * updated < 0:
            self._entries[order.symbol] = price
        self._positions[order.symbol] = updated
        self.fills.append(
            Fill(
                order.symbol,
                order.side,
                quantity,
                price,
                oid,
                client_order_id=order.client_order_id,
                trade_id=trade_id,
            )
        )

    async def place_orders(self, orders: Sequence[OrderRequest]) -> list[OrderResult]:
        result = []
        # Fresh placements share available observed liquidity within this batch.
        budgets: dict[tuple[str, str], list[list[Decimal]]] = {}
        for order in orders:
            wire_decimal(order.quantity)
            wire_decimal(order.price)
            if order.side not in {"buy", "sell"} or order.time_in_force not in {
                "Alo",
                "Gtc",
                "Ioc",
            }:
                raise ValueError("invalid paper order")
            book = self._books.get(order.symbol)
            if book is None or book.mid is None:
                result.append(OrderResult("rejected", error="book unavailable"))
                continue
            if order.client_order_id is not None and any(
                row.client_order_id == order.client_order_id for row in self._orders.values()
            ):
                result.append(OrderResult("rejected", error="duplicate active client_order_id"))
                continue
            if order.reduce_only:
                position = self._positions.get(order.symbol, Decimal(0))
                if (order.side == "buy" and position >= 0) or (
                    order.side == "sell" and position <= 0
                ):
                    result.append(
                        OrderResult("rejected", error="reduce-only would increase exposure")
                    )
                    continue
            crosses = (
                order.price >= book.asks[0].price
                if order.side == "buy"
                else order.price <= book.bids[0].price
            )
            if order.time_in_force == "Alo" and crosses:
                result.append(
                    OrderResult(
                        "rejected",
                        client_order_id=order.client_order_id,
                        error="post-only would cross",
                    )
                )
                continue
            self._next_id += 1
            oid = f"paper-{self._next_id}"
            key = (order.symbol, order.side)
            if key not in budgets:
                budgets[key] = [
                    [level.price, level.quantity]
                    for level in (book.asks if order.side == "buy" else book.bids)
                ]
            remaining = self._match(oid, order, budgets[key]) if crosses else order.quantity
            if remaining and order.time_in_force != "Ioc":
                self._rest(oid, replace(order, quantity=remaining))
                status = "resting"
            elif remaining == order.quantity:
                self._remember_terminal(oid, order)
                result.append(
                    OrderResult("rejected", oid, order.client_order_id, "no executable liquidity")
                )
                continue
            else:
                self._remember_terminal(oid, order)
                status = "filled"
            result.append(OrderResult(status, oid, order.client_order_id))
        return result

    async def cancel_orders(self, orders: Sequence[CancelRequest]) -> list[CancelResult]:
        results = []
        for request in orders:
            if (request.order_id is None) == (request.client_order_id is None):
                raise ValueError("cancel requires exactly one order_id or client_order_id")
            oid = request.order_id
            if oid is None:
                oid = next(
                    (
                        key
                        for key, row in self._orders.items()
                        if row.symbol == request.symbol
                        and row.client_order_id == request.client_order_id
                    ),
                    None,
                )
            found = oid in self._orders and self._orders[oid].symbol == request.symbol
            if found:
                self._remember_terminal(oid, self._remove(oid))
                status = "cancelled"
            else:
                if oid is None:
                    oid = next(
                        (
                            key
                            for key in reversed(self._terminal)
                            if self._terminal[key].symbol == request.symbol
                            and self._terminal[key].client_order_id == request.client_order_id
                        ),
                        None,
                    )
                terminal = self._terminal.get(oid)
                status = (
                    "already_closed"
                    if terminal is not None and terminal.symbol == request.symbol
                    else "rejected"
                )
            results.append(
                CancelResult(
                    status,
                    oid,
                    request.client_order_id,
                    "order not found" if status == "rejected" else None,
                )
            )
        return results

    async def cancel_all(self, symbol: str | None = None) -> None:
        for oid, request in list(self._orders.items()):
            if symbol is None or request.symbol == symbol:
                self._remember_terminal(oid, self._remove(oid))

    async def open_orders(self) -> list[OpenOrder]:
        return [
            OpenOrder(
                key,
                row.symbol,
                row.side,
                row.quantity,
                row.price,
                row.client_order_id,
                row.reduce_only,
            )
            for key, row in self._orders.items()
        ]

    async def account(self) -> Account:
        unrealized = sum(
            (
                position
                * ((self._books[symbol].mid or self._entries[symbol]) - self._entries[symbol])
                for symbol, position in self._positions.items()
            ),
            Decimal(0),
        )
        equity = self._cash + unrealized
        return Account(equity, dict(self._positions), equity)
