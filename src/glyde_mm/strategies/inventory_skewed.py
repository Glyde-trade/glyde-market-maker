# Copyright 2026 Glyde. Licensed under the Apache License, Version 2.0.
# Eta sizing adapted from Hummingbot (Apache-2.0), revision 9af100d6822d.
# Original implementation: hummingbot/strategy/avellaneda_market_making.
"""Avellaneda prices with exponential suppression of inventory-increasing quote size."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from ..models import Account, Book, OrderRequest
from .avellaneda_market_making import AvellanedaConfig, AvellanedaMarketMakingStrategy
from .pure_market_making import ONE, ZERO, StrategyContext, quantized_order, require_decimal


@dataclass(frozen=True)
class InventorySkewedConfig(AvellanedaConfig):
    order_amount_shape_factor: Decimal = ONE

    def __post_init__(self) -> None:
        super().__post_init__()
        require_decimal(self.order_amount_shape_factor, "order_amount_shape_factor")
        if self.order_amount_shape_factor > Decimal(100):
            raise ValueError("order_amount_shape_factor must be <= 100")


class InventorySkewedStrategy(AvellanedaMarketMakingStrategy):
    def __init__(self, context: StrategyContext, config: InventorySkewedConfig) -> None:
        super().__init__(context, config)
        self.inventory_config = config

    def quotes(self, book: Book, account: Account) -> tuple[OrderRequest, ...]:
        proposed = super().quotes(book, account)
        if not proposed or self.inventory_config.order_amount_shape_factor == ZERO:
            return proposed
        mid = book.mid
        assert mid is not None
        position = account.positions.get(book.symbol, ZERO)
        with localcontext() as context:
            context.prec = 50
            # Signed perpetual exposure replaces Hummingbot's spot inventory ratio.
            q = (position - self.model.inventory_target_position) * mid / account.equity
            factor = (-self.inventory_config.order_amount_shape_factor * abs(q)).exp()
        if q == ZERO:
            return proposed
        increasing_side = "buy" if q > ZERO else "sell"
        result = []
        for order in proposed:
            if order.side != increasing_side:
                result.append(order)
                continue
            shaped = quantized_order(self.context, order.side, order.price, order.quantity * factor)
            if shaped is not None:
                result.append(shaped)
        return tuple(result)
