"""Exact-decimal exchange models shared by strategies, safety and transports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from time import monotonic
from typing import Literal, Protocol

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class Level:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class Book:
    symbol: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    timestamp_ms: int
    received_at: float = field(default_factory=monotonic)

    @property
    def mid(self) -> Decimal | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2


@dataclass(frozen=True)
class Trade:
    """Public aggressor print; side names the taker, not the resting paper order."""

    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    timestamp_ms: int
    trade_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.symbol, str)
            or not 1 <= len(self.symbol) <= 64
            or self.side not in {"buy", "sell"}
            or type(self.timestamp_ms) is not int
            or self.timestamp_ms < 0
            or not isinstance(self.trade_id, str)
            or not self.trade_id
            or len(self.trade_id) > 64
            or any(not value.is_finite() or value <= 0 for value in (self.quantity, self.price))
        ):
            raise ValueError("invalid public trade")


@dataclass(frozen=True)
class Account:
    equity: Decimal
    positions: dict[str, Decimal] = field(default_factory=dict)
    available_balance: Decimal = Decimal("0")
    received_at: float = field(default_factory=monotonic)
    unrealized_pnl: Decimal | None = field(default=None, compare=False)


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    client_order_id: str | None = None
    reduce_only: bool = False
    time_in_force: Literal["Alo", "Gtc", "Ioc"] = "Alo"


@dataclass(frozen=True)
class CancelRequest:
    symbol: str
    order_id: str | None = None
    client_order_id: str | None = None


@dataclass(frozen=True)
class ModifyRequest:
    order_id: str
    order: OrderRequest


@dataclass(frozen=True)
class OrderResult:
    status: Literal["resting", "filled", "rejected", "unknown"]
    order_id: str | None = None
    client_order_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class CancelResult:
    status: Literal["cancelled", "rejected", "unknown", "already_closed"]
    order_id: str | None = None
    client_order_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    order_id: str
    fee: Decimal = Decimal("0")
    client_order_id: str | None = None
    trade_id: str | None = None


class TradingClient(Protocol):
    async def place_orders(self, orders: Sequence[OrderRequest]) -> list[OrderResult]: ...
    async def cancel_orders(self, orders: Sequence[CancelRequest]) -> list[CancelResult]: ...
    async def cancel_all(self, symbol: str | None = None) -> None: ...
    async def account(self) -> Account: ...
    async def open_orders(self) -> list[OpenOrder]: ...


@dataclass(frozen=True)
class OpenOrder:
    order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    client_order_id: str | None = None
    reduce_only: bool = False
