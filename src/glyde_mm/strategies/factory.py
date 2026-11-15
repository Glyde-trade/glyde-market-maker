"""Convert the public YAML strategy mapping without coercing boolean/integer flags."""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal, InvalidOperation
from typing import Any

from .avellaneda_market_making import AvellanedaConfig, AvellanedaMarketMakingStrategy
from .grid import GridConfig, GridStrategy
from .pure_market_making import (
    PureMarketMakingConfig,
    PureMarketMakingStrategy,
    StrategyContext,
)


def parse_config(cls: type, values: dict[str, Any]):
    definitions = {field.name: field for field in fields(cls)}
    if not isinstance(values, dict) or set(values) - definitions.keys():
        raise ValueError("unknown strategy configuration field")
    converted = dict(values)
    for key, value in values.items():
        default = definitions[key].default
        if isinstance(default, Decimal) or key in {
            "price_floor",
            "price_ceiling",
            "lower_price",
            "upper_price",
            "order_amount",
        }:
            if value is None and default is None:
                continue
            try:
                converted[key] = Decimal(str(value))
            except InvalidOperation:
                raise ValueError(f"{key} must be a decimal") from None
        elif key == "order_refresh_time":
            if isinstance(value, bool):
                raise ValueError("order_refresh_time must be a number")
            converted[key] = float(value)
    return cls(**converted)


def create_strategy(name: str, context: StrategyContext, values: dict[str, Any]):
    if name == "avellaneda_market_making":
        return AvellanedaMarketMakingStrategy(context, parse_config(AvellanedaConfig, values))
    if name == "grid":
        return GridStrategy(context, parse_config(GridConfig, values))
    if name != "pure_market_making":
        raise ValueError(f"unsupported strategy: {name}")
    return PureMarketMakingStrategy(context, parse_config(PureMarketMakingConfig, values))
