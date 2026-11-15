# Copyright 2026 Glyde. Licensed under the Apache License, Version 2.0.
# Strategy logic adapted from Hummingbot (Apache-2.0), revision 9af100d6822d.
# Original implementation: hummingbot/strategy/avellaneda_market_making.
"""Hummingbot-style normalized Avellaneda pricing with explicit liquidity calibration."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal, localcontext

from glyde_mm.models import Account, Book, OrderRequest

from .pure_market_making import (
    ONE,
    TWO,
    ZERO,
    PureMarketMakingConfig,
    PureMarketMakingStrategy,
    StrategyContext,
    quantized_order,
    require_decimal,
    valid_book,
)


@dataclass(frozen=True)
class AvellanedaConfig:
    order_amount: Decimal = Decimal("0.001")
    order_refresh_time: float = 30.0
    refresh_tolerance_pct: Decimal | None = None
    max_order_age: float = 300.0
    order_levels: int = 1
    order_level_spread: Decimal = Decimal("0.001")
    order_level_amount: Decimal = ZERO
    minimum_spread: Decimal = Decimal("0.001")
    inventory_target_position: Decimal = ZERO
    risk_factor: Decimal = ONE
    liquidity_kappa: Decimal = ONE
    time_left_fraction: Decimal = ONE
    volatility_samples: int = 30
    sample_interval_ms: int = 1000
    price_ceiling: Decimal | None = None
    price_floor: Decimal | None = None

    def lifecycle_config(self) -> PureMarketMakingConfig:
        return PureMarketMakingConfig(
            order_amount=self.order_amount,
            order_refresh_time=self.order_refresh_time,
            refresh_tolerance_pct=self.refresh_tolerance_pct,
            max_order_age=self.max_order_age,
            order_levels=self.order_levels,
            order_level_spread=self.order_level_spread,
            order_level_amount=self.order_level_amount,
            minimum_spread=self.minimum_spread,
            inventory_target_position=self.inventory_target_position,
            price_floor=self.price_floor,
            price_ceiling=self.price_ceiling,
        )

    def __post_init__(self) -> None:
        self.lifecycle_config()
        for name in ("risk_factor", "liquidity_kappa", "time_left_fraction"):
            value = getattr(self, name)
            require_decimal(value, name)
            if value == ZERO:
                raise ValueError(f"{name} must be positive")
        if self.time_left_fraction > ONE:
            raise ValueError("time_left_fraction must be <= 1")
        if type(self.volatility_samples) is not int or not 2 <= self.volatility_samples <= 4096:
            raise ValueError("volatility_samples must be an integer in 2..4096")
        if type(self.sample_interval_ms) is not int or not 1 <= self.sample_interval_ms <= 60000:
            raise ValueError("sample_interval_ms must be an integer in 1..60000")


class AvellanedaMarketMakingStrategy(PureMarketMakingStrategy):
    def __init__(self, context: StrategyContext, config: AvellanedaConfig) -> None:
        super().__init__(context, config.lifecycle_config())
        self.model = config
        self._prices: deque[Decimal] = deque(maxlen=config.volatility_samples + 1)
        self._sample_bucket: int | None = None

    @property
    def volatility(self) -> Decimal | None:
        """RMS absolute midpoint change per configured sampling interval, not percent."""
        if len(self._prices) != self.model.volatility_samples + 1:
            return None
        prices = tuple(self._prices)
        with localcontext() as context:
            context.prec = 50
            variance = sum(
                ((b - a) ** 2 for a, b in zip(prices[:-1], prices[1:], strict=True)), ZERO
            )
            value = (variance / Decimal(self.model.volatility_samples)).sqrt()
        return value if value > ZERO else None

    async def on_book(self, book: Book) -> None:
        ready = self.volatility is not None
        await super().on_book(book)
        if book.symbol != self.context.symbol:
            return
        if self.book is None:
            self._prices.clear()
            self._sample_bucket = None
        elif self.book is book:
            bucket = book.timestamp_ms // self.model.sample_interval_ms
            if self._sample_bucket is None or bucket > self._sample_bucket:
                if self._sample_bucket is not None and bucket != self._sample_bucket + 1:
                    self._prices.clear()
                mid = book.mid
                assert mid is not None
                self._prices.append(mid)
                self._sample_bucket = bucket
        if ready != (self.volatility is not None):
            self._needs_refresh = True

    def model_prices(self, book: Book, account: Account) -> tuple[Decimal, Decimal] | None:
        if not valid_book(book, self.context.symbol):
            return None
        volatility = self.volatility
        if volatility is None:
            return None
        require_decimal(account.equity, "equity")
        if account.equity == ZERO:
            raise ValueError("positive equity required for inventory normalization")
        position = account.positions.get(book.symbol, ZERO)
        if not isinstance(position, Decimal) or not position.is_finite():
            raise ValueError("finite position required")
        mid = book.mid
        assert mid is not None
        with localcontext() as context:
            context.prec = 50
            q = (position - self.model.inventory_target_position) * mid / account.equity
            risk = self.model.risk_factor * volatility * self.model.time_left_fraction
            reservation = mid - q * risk
            spread = (
                risk
                + TWO
                / self.model.risk_factor
                * (ONE + self.model.risk_factor / self.model.liquidity_kappa).ln()
            )
        return reservation, spread

    def quotes(self, book: Book, account: Account) -> tuple[OrderRequest, ...]:
        model = self.model_prices(book, account)
        if model is None:
            return ()
        reservation, spread = model
        mid = book.mid
        assert mid is not None
        bid = min(reservation - spread / TWO, mid * (ONE - self.model.minimum_spread))
        ask = max(reservation + spread / TWO, mid * (ONE + self.model.minimum_spread))
        result = []
        used: set[tuple[str, Decimal]] = set()
        for level in range(self.model.order_levels):
            distance = mid * self.model.order_level_spread * level
            quantity = self.model.order_amount + self.model.order_level_amount * level
            for side, price in (("buy", bid - distance), ("sell", ask + distance)):
                if side == "buy" and self.model.price_ceiling is not None:
                    if mid >= self.model.price_ceiling:
                        continue
                if side == "sell" and self.model.price_floor is not None:
                    if mid <= self.model.price_floor:
                        continue
                order = quantized_order(self.context, side, price, quantity)
                if order is not None and (side, order.price) not in used:
                    used.add((side, order.price))
                    result.append(order)
        return tuple(result)
