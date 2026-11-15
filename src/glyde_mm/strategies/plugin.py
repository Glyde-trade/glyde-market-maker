"""Trusted local strategy code supplies intents; the host retains execution and risk."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import math
import re
import types
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Generic, TypeVar, Union, get_args, get_origin, get_type_hints

from glyde_mm.models import Account, Book, Fill, OrderRequest, Side
from glyde_mm.risk import RiskManagedClient

from .pure_market_making import (
    PureMarketMakingConfig,
    PureMarketMakingStrategy,
    StrategyContext,
    quantized_order,
)

Parameters = TypeVar("Parameters")


@dataclass(frozen=True)
class PluginContext:
    symbol: str
    price_tick: Decimal
    quantity_step: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class Quote:
    side: Side
    quantity: Decimal
    price: Decimal

    def __post_init__(self) -> None:
        if self.side not in {"buy", "sell"} or any(
            not isinstance(value, Decimal) or not value.is_finite() or value <= 0
            for value in (self.quantity, self.price)
        ):
            raise ValueError("plugin quotes require a side and positive finite Decimals")


class StrategyPlugin(ABC, Generic[Parameters]):
    """Nonblocking hooks have no execution API; quotes are evaluated at the execution cut."""

    config_type: type

    def __init__(self, config: Parameters) -> None:
        self.config = config

    async def on_start(self, context: PluginContext) -> None:
        pass

    async def on_book(self, book: Book) -> None:
        pass

    async def on_fill(self, fill: Fill) -> None:
        pass

    async def on_tick(self, now: float) -> None:
        pass

    @abstractmethod
    def quotes(self, context: PluginContext, book: Book, account: Account) -> Sequence[Quote]:
        """Return the complete desired quote set; an empty sequence pulls all quotes."""

    async def on_stop(self) -> None:
        pass


def _scalar(kind: type, value: object, name: str):
    if get_origin(kind) in {Union, types.UnionType}:
        alternatives = get_args(kind)
        if len(alternatives) == 2 and type(None) in alternatives:
            member = next(item for item in alternatives if item is not type(None))
            return None if value is None else _scalar(member, value, name)
    if kind is Decimal:
        if type(value) not in {str, int, float, Decimal}:
            raise ValueError(f"{name} must be a decimal")
        try:
            result = Decimal(str(value))
        except InvalidOperation:
            raise ValueError(f"{name} must be a decimal") from None
        if result.is_finite():
            return result
    elif kind is float:
        if type(value) in {int, float} and math.isfinite(value):
            return float(value)
    elif kind in {bool, int, str} and type(value) is kind:
        return value
    raise ValueError(f"invalid type or value for plugin parameter {name}")


def _supported(kind: type) -> bool:
    if kind in {Decimal, float, bool, int, str}:
        return True
    args = get_args(kind)
    return (
        get_origin(kind) in {Union, types.UnionType}
        and len(args) == 2
        and type(None) in args
        and _supported(next(item for item in args if item is not type(None)))
    )


def parse_parameters(config_type: type, values: dict) -> object:
    if (
        not isinstance(config_type, type)
        or not is_dataclass(config_type)
        or not config_type.__dataclass_params__.frozen
    ):
        raise ValueError("plugin config_type must be a frozen dataclass")
    definitions = {field.name for field in fields(config_type)}
    hints = get_type_hints(config_type)
    if (
        not isinstance(values, dict)
        or set(values) - definitions
        or definitions - hints.keys()
        or any(not _supported(hints[name]) for name in definitions)
    ):
        raise ValueError("unknown plugin parameter or unsupported config annotation")
    try:
        result = config_type(**{k: _scalar(hints[k], v, k) for k, v in values.items()})
    except TypeError as error:
        raise ValueError("missing or invalid plugin parameters") from error
    # Dataclass defaults must obey the same contract as YAML values.
    for name in definitions:
        value = getattr(result, name)
        if type(_scalar(hints[name], value, name)) is not type(value):
            raise ValueError(f"plugin default {name} must match its annotation")
    return result


@dataclass(frozen=True)
class PluginDefinition:
    plugin_type: type[StrategyPlugin]
    parameters: object
    execution: PureMarketMakingConfig


def load_plugin(name: str, values: dict) -> PluginDefinition:
    parts = name.split(":")
    if (
        len(parts) != 3
        or parts[0] != "plugin"
        or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", parts[1], re.ASCII)
        or not re.fullmatch(r"[A-Za-z_]\w*", parts[2], re.ASCII)
    ):
        raise ValueError("plugin strategy must be plugin:module:Class")
    if not isinstance(values, dict) or set(values) - {
        "parameters",
        "order_refresh_time",
        "max_order_age",
        "refresh_tolerance_pct",
    }:
        raise ValueError("unknown plugin host configuration field")
    execution = PureMarketMakingConfig(
        **{
            key: _scalar(Decimal if key == "refresh_tolerance_pct" else float, value, key)
            if value is not None or key != "refresh_tolerance_pct"
            else None
            for key, value in values.items()
            if key != "parameters"
        }
    )
    plugin_type = getattr(importlib.import_module(parts[1]), parts[2])
    if (
        not isinstance(plugin_type, type)
        or not issubclass(plugin_type, StrategyPlugin)
        or inspect.isabstract(plugin_type)
    ):
        raise ValueError("plugin class must implement StrategyPlugin")
    parameters = parse_parameters(plugin_type.config_type, values.get("parameters", {}))
    return PluginDefinition(plugin_type, parameters, execution)


class PluginStrategy(PureMarketMakingStrategy):
    """Reuse the host's order coordinator, passing only copied data to plugin hooks."""

    def __init__(self, context: StrategyContext, definition: PluginDefinition) -> None:
        if not isinstance(context.client, RiskManagedClient):
            raise ValueError("strategy plugins require the host RiskManagedClient")
        super().__init__(context, definition.execution)
        self._plugin = definition.plugin_type(definition.parameters)
        self._plugin_lock = asyncio.Lock()
        self._started = False
        self._stopped = False

    def _view(self) -> PluginContext:
        return PluginContext(
            self.context.symbol,
            self.context.price_tick,
            self.context.quantity_step,
            self.context.min_notional,
        )

    async def _hook(self, name: str, *args: object) -> None:
        try:
            async with asyncio.timeout(5), self._plugin_lock:
                await getattr(self._plugin, name)(*args)
        except BaseException:
            self.blocked = True
            raise

    async def on_book(self, book: Book) -> None:
        await super().on_book(book)
        if self.book is not book or self.blocked:
            return
        if not self._started:
            self._started = True
            await self._hook("on_start", self._view())
        await self._hook("on_book", book)

    async def on_fill(self, fill: Fill) -> None:
        if fill.symbol != self.context.symbol or self.blocked:
            return
        await super().on_fill(fill)
        await self._hook("on_fill", fill)

    async def on_tick(self, now: float | None = None) -> None:
        if self.blocked:
            return
        now = self.context.clock() if now is None else now
        if not math.isfinite(now) or now < self._last_tick:
            raise ValueError("tick time must be finite and monotonic")
        await self._hook("on_tick", now)
        await super().on_tick(now)

    def quotes(self, book: Book, account: Account) -> tuple[OrderRequest, ...]:
        desired = self._plugin.quotes(
            self._view(), book, replace(account, positions=dict(account.positions))
        )
        if not isinstance(desired, (list, tuple)) or len(desired) > 100:
            raise ValueError("plugin must return at most 100 Quote intents as a list or tuple")
        orders = []
        for intent in desired:
            if type(intent) is not Quote:
                raise ValueError("plugin must return Quote intents, not execution requests")
            order = quantized_order(self.context, intent.side, intent.price, intent.quantity)
            if order is not None:
                orders.append(order)
        return tuple(orders)

    async def stop(self) -> None:
        try:
            await super().stop()
        finally:
            if self._started and not self._stopped:
                self._stopped = True
                await self._hook("on_stop")
