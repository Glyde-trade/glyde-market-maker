"""Convert the public YAML strategy mapping without coercing boolean/integer flags."""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal, InvalidOperation
from typing import Any, TypeAlias, TypeVar, cast

from .avellaneda_market_making import AvellanedaConfig, AvellanedaMarketMakingStrategy
from .grid import GridConfig, GridStrategy
from .inventory_skewed import InventorySkewedConfig, InventorySkewedStrategy
from .plugin import PluginDefinition, PluginStrategy, load_plugin
from .pure_market_making import (
    PureMarketMakingConfig,
    PureMarketMakingStrategy,
    StrategyContext,
)

StrategyConfig: TypeAlias = (
    PureMarketMakingConfig | GridConfig | AvellanedaConfig | InventorySkewedConfig
)
Strategy: TypeAlias = PureMarketMakingStrategy | GridStrategy
ConfigT = TypeVar("ConfigT", bound=StrategyConfig)


def parse_config(cls: type[ConfigT], values: dict[str, Any]) -> ConfigT:
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
            "refresh_tolerance_pct",
            "order_refresh_tolerance_pct",
            "inventory_target_base_ratio",
        }:
            if value is None and default is None:
                continue
            try:
                converted[key] = Decimal(str(value))
            except InvalidOperation:
                raise ValueError(f"{key} must be a decimal") from None
        elif key in {"order_refresh_time", "max_order_age", "filled_order_delay"}:
            if isinstance(value, bool):
                raise ValueError(f"{key} must be a number")
            converted[key] = float(value)
    return cast(ConfigT, cls(**converted))


_CONFIGS: dict[str, type[StrategyConfig]] = {
    "inventory_skewed": InventorySkewedConfig,
    "avellaneda_market_making": AvellanedaConfig,
    "grid": GridConfig,
    "pure_market_making": PureMarketMakingConfig,
}


def validate_strategy_config(
    name: str, values: dict[str, Any]
) -> StrategyConfig | PluginDefinition:
    if name.startswith("plugin:"):
        return load_plugin(name, values)
    try:
        config_type = _CONFIGS[name]
    except KeyError:
        raise ValueError("unsupported strategy") from None
    return parse_config(config_type, values)


def create_strategy(name: str, context: StrategyContext, values: dict[str, Any]) -> Strategy:
    selected = validate_strategy_config(name, values)
    if isinstance(selected, PluginDefinition):
        return PluginStrategy(context, selected)
    if isinstance(selected, InventorySkewedConfig):
        return InventorySkewedStrategy(context, selected)
    if isinstance(selected, AvellanedaConfig):
        return AvellanedaMarketMakingStrategy(context, selected)
    if isinstance(selected, GridConfig):
        return GridStrategy(context, selected)
    return PureMarketMakingStrategy(context, selected)
