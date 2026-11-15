"""Convert the public YAML strategy mapping without coercing boolean/integer flags."""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal, InvalidOperation
from typing import Any

from .avellaneda_market_making import AvellanedaConfig, AvellanedaMarketMakingStrategy
from .grid import GridConfig, GridStrategy
from .inventory_skewed import InventorySkewedConfig, InventorySkewedStrategy
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


_STRATEGIES = {
    "inventory_skewed": (InventorySkewedStrategy, InventorySkewedConfig),
    "avellaneda_market_making": (AvellanedaMarketMakingStrategy, AvellanedaConfig),
    "grid": (GridStrategy, GridConfig),
    "pure_market_making": (PureMarketMakingStrategy, PureMarketMakingConfig),
}


def validate_strategy_config(name: str, values: dict[str, Any]):
    try:
        _, config_type = _STRATEGIES[name]
    except KeyError:
        raise ValueError("unsupported strategy") from None
    return parse_config(config_type, values)


def create_strategy(name: str, context: StrategyContext, values: dict[str, Any]):
    selected = validate_strategy_config(name, values)
    strategy_type, _ = _STRATEGIES[name]
    return strategy_type(context, selected)
