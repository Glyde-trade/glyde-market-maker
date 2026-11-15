# Copyright 2026 Glyde. Licensed under the Apache License, Version 2.0.
# Grid-level design reference: Hummingbot grid_executor (Apache-2.0), revision 9af100d6822d.
"""A fixed-range grid using the shared cancel-before-replace PMM lifecycle."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from ..models import Account, Book, OrderRequest
from .pure_market_making import (
    PureMarketMakingConfig,
    PureMarketMakingStrategy,
    StrategyContext,
    quantized_order,
    valid_book,
)


@dataclass(frozen=True)
class GridConfig:
    lower_price: Decimal
    upper_price: Decimal
    order_amount: Decimal
    levels: int = 5
    order_refresh_time: float = 30.0
    minimum_spread: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for name in ("lower_price", "upper_price", "order_amount"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        if self.lower_price >= self.upper_price:
            raise ValueError("lower_price must be below upper_price")
        if type(self.levels) is not int or not 2 <= self.levels <= 50:
            raise ValueError("levels must be an integer from 2 to 50")
        if (
            not isinstance(self.minimum_spread, Decimal)
            or not self.minimum_spread.is_finite()
            or not 0 <= self.minimum_spread < 1
        ):
            raise ValueError("minimum_spread must be a Decimal fraction from 0 to below 1")
        if not math.isfinite(self.order_refresh_time) or self.order_refresh_time <= 0:
            raise ValueError("order_refresh_time must be positive and finite")


class GridStrategy(PureMarketMakingStrategy):
    """Quote fixed grid levels on both sides of the market, through the risk wrapper only."""

    def __init__(self, context: StrategyContext, config: GridConfig) -> None:
        self.grid = config
        super().__init__(
            context,
            PureMarketMakingConfig(
                order_amount=config.order_amount,
                order_refresh_time=config.order_refresh_time,
                minimum_spread=config.minimum_spread,
                price_floor=config.lower_price,
                price_ceiling=config.upper_price,
                hanging_orders_enabled=False,
            ),
        )

    def _must_replace(self, book: Book) -> bool:
        mid = book.mid
        return (
            mid is None
            or not self.grid.lower_price < mid < self.grid.upper_price
            or super()._must_replace(book)
        )

    def quotes(self, book: Book, account: Account) -> tuple[OrderRequest, ...]:
        if not valid_book(book, self.context.symbol):
            return ()
        mid = book.mid
        if mid is None or not self.grid.lower_price < mid < self.grid.upper_price:
            return ()
        step = (self.grid.upper_price - self.grid.lower_price) / Decimal(self.grid.levels - 1)
        result: list[OrderRequest] = []
        used: set[tuple[str, Decimal]] = set()
        for index in range(self.grid.levels):
            price = self.grid.lower_price + step * index
            if price == mid or abs(price - mid) / mid < self.grid.minimum_spread:
                continue
            side = "buy" if price < mid else "sell"
            order = quantized_order(self.context, side, price, self.grid.order_amount)
            if (
                order is None
                or (side, order.price) in used
                or not self.grid.lower_price <= order.price <= self.grid.upper_price
            ):
                continue
            if (side == "buy" and order.price >= book.asks[0].price) or (
                side == "sell" and order.price <= book.bids[0].price
            ):
                continue
            used.add((side, order.price))
            result.append(order)
        return tuple(result)
