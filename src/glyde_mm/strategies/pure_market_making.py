# Copyright 2026 Glyde. Licensed under the Apache License, Version 2.0.
# Strategy logic adapted from Hummingbot (Apache-2.0), revision 9af100d6822d.
# Original implementation: hummingbot/strategy/pure_market_making.
"""Decimal quote planning and cancel-before-replace lifecycle for perpetual markets."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from time import monotonic

from glyde_mm.client import RateLimited
from glyde_mm.models import (
    Account,
    Book,
    CancelRequest,
    Fill,
    OrderRequest,
    TradingClient,
    bot_client_order_id,
)
from glyde_mm.risk import RiskViolation

ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")


def require_decimal(value: Decimal, name: str, *, minimum: Decimal = ZERO) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < minimum:
        raise ValueError(f"{name} must be a finite Decimal >= {minimum}")


@dataclass(frozen=True)
class StrategyContext:
    """The client must be risk-managed; strategies never access credentials or signing."""

    client: TradingClient
    symbol: str
    price_tick: Decimal
    quantity_step: Decimal
    min_notional: Decimal = ZERO
    clock: Callable[[], float] = monotonic
    quote_center: Callable[[Book], Decimal] | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        for name in ("price_tick", "quantity_step"):
            value = getattr(self, name)
            require_decimal(value, name)
            if value == ZERO:
                raise ValueError(f"{name} must be positive")
        require_decimal(self.min_notional, "min_notional")


RATE_BACKOFF_SECONDS = 30.0


@dataclass(frozen=True)
class PureMarketMakingConfig:
    """Spread fractions: 0.01 means 1%; amounts and target inventory are base units."""

    bid_spread: Decimal = Decimal("0.001")
    ask_spread: Decimal = Decimal("0.001")
    order_amount: Decimal = Decimal("0.001")
    order_refresh_time: float = 30.0
    refresh_tolerance_pct: Decimal | None = None
    order_refresh_tolerance_pct: Decimal | None = None
    filled_order_delay: float = 0.0
    max_order_age: float = 300.0
    order_levels: int = 1
    order_level_spread: Decimal = Decimal("0.001")
    order_level_amount: Decimal = ZERO
    inventory_skew_enabled: bool = True
    inventory_target_position: Decimal = ZERO
    inventory_target_base_ratio: Decimal | None = None
    inventory_range_multiplier: Decimal = ONE
    minimum_spread: Decimal = ZERO
    price_ceiling: Decimal | None = None
    price_floor: Decimal | None = None
    ping_pong_enabled: bool = False
    hanging_orders_enabled: bool = False
    reference_source: str = "glyde"
    reference_skew_bps: Decimal = ZERO
    max_reference_deviation_bps: Decimal = Decimal("50")
    reference_stale_seconds: Decimal = Decimal("3")
    # replace_all cancels and re-places every quote on refresh; changed_levels keeps quotes still
    # within the refresh tolerance (exact price when unset) and replaces only the rest.
    quote_refresh: str = "replace_all"
    # Cap on order entries placed plus cancelled per rolling minute by refreshes; None is no cap.
    # Live runs that leave it unset get DEFAULT_QUOTE_ACTIONS_PER_MINUTE (see runner).
    max_quote_actions_per_minute: int | None = None

    def __post_init__(self) -> None:
        validate_refresh_settings(self.refresh_tolerance_pct, self.max_order_age)
        validate_refresh_settings(self.order_refresh_tolerance_pct, self.max_order_age)
        if self.refresh_tolerance_pct is not None and self.order_refresh_tolerance_pct is not None:
            raise ValueError("configure only one refresh tolerance name")
        if (
            isinstance(self.filled_order_delay, bool)
            or not math.isfinite(self.filled_order_delay)
            or not 0 <= self.filled_order_delay <= 86400
        ):
            raise ValueError("filled_order_delay must be finite seconds in 0..86400")
        if self.inventory_target_base_ratio is not None:
            require_decimal(self.inventory_target_base_ratio, "inventory_target_base_ratio")
            if self.inventory_target_base_ratio > ONE or self.inventory_target_position != ZERO:
                raise ValueError(
                    "base ratio must be in 0..1 and cannot combine with position target"
                )
        if self.quote_refresh not in {"replace_all", "changed_levels"}:
            raise ValueError("quote_refresh must be replace_all or changed_levels")
        cap = self.max_quote_actions_per_minute
        if cap is not None and (type(cap) is not int or not 1 <= cap <= 6000):
            raise ValueError("max_quote_actions_per_minute must be an integer in 1..6000")
        if self.reference_source not in {"glyde", "hyperliquid"}:
            raise ValueError("reference_source must be glyde or hyperliquid")
        require_decimal(self.reference_skew_bps, "reference_skew_bps", minimum=Decimal("-1000"))
        if self.reference_skew_bps > 1000:
            raise ValueError("reference_skew_bps must be within -1000..1000")
        for name, maximum in (
            ("max_reference_deviation_bps", 1000),
            ("reference_stale_seconds", 60),
        ):
            value = getattr(self, name)
            require_decimal(value, name)
            if not 0 < value <= maximum:
                raise ValueError(f"{name} outside supported range")
        if self.reference_source == "glyde" and self.reference_skew_bps != ZERO:
            raise ValueError("fixed reference skew requires the hyperliquid reference")
        for name in (
            "bid_spread",
            "ask_spread",
            "minimum_spread",
            "order_level_spread",
        ):
            value = getattr(self, name)
            require_decimal(value, name)
            if value >= ONE:
                raise ValueError(f"{name} must be a fraction below 1")
        for name in (
            "order_amount",
            "order_level_amount",
            "inventory_range_multiplier",
        ):
            require_decimal(getattr(self, name), name)
        if self.order_amount == ZERO or self.inventory_range_multiplier == ZERO:
            raise ValueError("order_amount and inventory_range_multiplier must be positive")
        if (
            type(self.order_levels) is not int
            or not 1 <= self.order_levels <= 50
            or not math.isfinite(self.order_refresh_time)
            or self.order_refresh_time <= 0
        ):
            raise ValueError("positive refresh interval and 1..50 levels required")
        if not isinstance(self.inventory_target_position, Decimal) or not (
            self.inventory_target_position.is_finite()
        ):
            raise ValueError("inventory_target_position must be a finite signed Decimal")
        if (
            type(self.inventory_skew_enabled) is not bool
            or type(self.ping_pong_enabled) is not bool
            or self.hanging_orders_enabled is not False
        ):
            raise ValueError(
                "inventory skew/ping-pong must be boolean; hanging orders are unsupported"
            )
        for name in ("price_floor", "price_ceiling"):
            value = getattr(self, name)
            if value is not None:
                require_decimal(value, name)
                if value == ZERO:
                    raise ValueError(f"{name} must be positive")
        if (
            self.price_floor is not None
            and self.price_ceiling is not None
            and self.price_floor >= self.price_ceiling
        ):
            raise ValueError("price_floor must be below price_ceiling")

    @property
    def price_refresh_tolerance(self) -> Decimal | None:
        return (
            self.order_refresh_tolerance_pct
            if self.order_refresh_tolerance_pct is not None
            else self.refresh_tolerance_pct
        )


def validate_refresh_settings(tolerance: Decimal | None, max_age: float) -> None:
    if tolerance is not None:
        require_decimal(tolerance, "refresh_tolerance_pct")
        if tolerance > 100:
            raise ValueError("refresh_tolerance_pct must be <= 100")
    if isinstance(max_age, bool) or not math.isfinite(max_age) or not 0 < max_age <= 86400:
        raise ValueError("max_order_age must be positive and at most 86400 seconds")


def valid_book(book: Book, symbol: str) -> bool:
    if book.symbol != symbol or not book.bids or not book.asks:
        return False
    bid, ask = book.bids[0], book.asks[0]
    return (
        all(
            isinstance(value, Decimal) and value.is_finite() and value > ZERO
            for value in (bid.price, ask.price, bid.quantity, ask.quantity)
        )
        and bid.price < ask.price
    )


def inventory_ratios(
    position: Decimal,
    config: PureMarketMakingConfig,
    *,
    equity: Decimal | None = None,
    mid: Decimal | None = None,
) -> tuple[Decimal, Decimal]:
    """Hummingbot's linear size skew, adapted to a signed perpetual position target."""
    if not isinstance(position, Decimal) or not position.is_finite():
        raise ValueError("position must be a finite signed Decimal")
    if not config.inventory_skew_enabled:
        return ONE, ONE
    total_size = TWO * sum(
        (
            config.order_amount + config.order_level_amount * level
            for level in range(config.order_levels)
        ),
        ZERO,
    )
    target = config.inventory_target_position
    width = total_size * config.inventory_range_multiplier
    if config.inventory_target_base_ratio is not None:
        if equity is None or mid is None or not equity.is_finite() or equity <= ZERO or mid <= ZERO:
            return ZERO, ZERO
        # Perpetual inventory is signed position value / equity, not a spot balance ratio.
        target = equity * config.inventory_target_base_ratio / mid
        width = min(width, equity / (TWO * mid))
    distance = (target - position) / width
    bid = max(ZERO, min(TWO, ONE + distance))
    return bid, TWO - bid


def quantized_order(
    context: StrategyContext, side: str, price: Decimal, quantity: Decimal
) -> OrderRequest | None:
    if not price.is_finite() or price <= ZERO or not quantity.is_finite() or quantity <= ZERO:
        return None
    rounding = ROUND_FLOOR if side == "buy" else ROUND_CEILING
    price = (price / context.price_tick).to_integral_value(rounding=rounding) * context.price_tick
    quantity = (quantity / context.quantity_step).to_integral_value(
        rounding=ROUND_FLOOR
    ) * context.quantity_step
    if price <= ZERO or quantity <= ZERO or price * quantity < context.min_notional:
        return None
    return OrderRequest(context.symbol, side, quantity, price)  # type: ignore[arg-type]


class PureMarketMakingStrategy:
    def __init__(self, context: StrategyContext, config: PureMarketMakingConfig) -> None:
        self.context = context
        self.config = config
        self.book: Book | None = None
        self._book_cut: tuple[int, float] | None = None
        self.blocked = False
        self._active: dict[str, OrderRequest] = {}
        self._last_refresh: float | None = None
        self._active_since: float | None = None
        self._last_band: tuple[bool, bool] | None = None
        self._last_tick = -math.inf
        self._fill_generation = 0
        self._ping_pong_side: str | None = None
        self._ping_fills: set[tuple[str, str]] = set()
        self._needs_refresh = False
        self._quote_after = 0.0
        self._placed_at: dict[str, float] = {}
        self._action_times: list[float] = []
        self._throttled_until = 0.0
        self._rate_backoff = 0.0
        self._lock = asyncio.Lock()

    def _quote_mid(self, book: Book) -> Decimal | None:
        if self.config.reference_source == "hyperliquid":
            if self.context.quote_center is None:
                raise RiskViolation("reference_unavailable")
            return self.context.quote_center(book)
        return book.mid

    def quotes(self, book: Book, account: Account) -> tuple[OrderRequest, ...]:
        if not valid_book(book, self.context.symbol):
            return ()
        mid = self._quote_mid(book)
        assert mid is not None
        bid_ratio, ask_ratio = inventory_ratios(
            account.positions.get(book.symbol, ZERO), self.config, equity=account.equity, mid=mid
        )
        orders = []
        for level in range(self.config.order_levels):
            amount = self.config.order_amount + self.config.order_level_amount * level
            for side, spread, ratio in (
                ("buy", self.config.bid_spread, bid_ratio),
                ("sell", self.config.ask_spread, ask_ratio),
            ):
                if self.config.ping_pong_enabled and self._ping_pong_side not in (None, side):
                    continue
                if side == "buy" and self.config.price_ceiling is not None:
                    if mid >= self.config.price_ceiling:
                        continue
                if side == "sell" and self.config.price_floor is not None:
                    if mid <= self.config.price_floor:
                        continue
                distance = max(
                    spread + level * self.config.order_level_spread,
                    self.config.minimum_spread,
                )
                price = mid * (ONE - distance if side == "buy" else ONE + distance)
                order = quantized_order(self.context, side, price, amount * ratio)
                if order is not None:
                    if self.config.reference_source == "hyperliquid" and (
                        (side == "buy" and order.price >= book.asks[0].price)
                        or (side == "sell" and order.price <= book.bids[0].price)
                    ):
                        continue
                    orders.append(order)
        return tuple(orders)

    async def on_book(self, book: Book) -> None:
        if book.symbol != self.context.symbol:
            return
        if (
            type(book.timestamp_ms) is not int
            or book.timestamp_ms < 0
            or not math.isfinite(book.received_at)
        ):
            self.book = None
            return
        if self._book_cut is not None and (
            book.timestamp_ms < self._book_cut[0] or book.received_at < self._book_cut[1]
        ):
            return
        self._book_cut = book.timestamp_ms, book.received_at
        self.book = book if valid_book(book, self.context.symbol) else None

    async def on_fill(self, fill: Fill) -> None:
        if fill.symbol == self.context.symbol:
            if self.config.ping_pong_enabled or self.config.filled_order_delay > 0:
                order = self._active.get(fill.order_id)
                if order is None:
                    return
                if (
                    fill.side != order.side
                    or fill.client_order_id not in (None, order.client_order_id)
                    or not isinstance(fill.trade_id, str)
                    or not 1 <= len(fill.trade_id) <= 128
                    or not isinstance(fill.quantity, Decimal)
                    or not fill.quantity.is_finite()
                    or not ZERO < fill.quantity <= order.quantity
                    or not isinstance(fill.price, Decimal)
                    or not fill.price.is_finite()
                    or fill.price <= ZERO
                    or (fill.side == "buy" and fill.price > order.price)
                    or (fill.side == "sell" and fill.price < order.price)
                ):
                    self.blocked = True
                    raise RiskViolation(
                        "ping_pong_fill_evidence"
                        if self.config.ping_pong_enabled
                        else "delayed_fill_evidence"
                    )
                identity = fill.order_id, fill.trade_id
                if identity in self._ping_fills:
                    return
                if len(self._ping_fills) >= 4096:
                    self.blocked = True
                    raise RiskViolation("ping_pong_fill_budget")
                self._ping_fills.add(identity)
                if self.config.ping_pong_enabled:
                    self._ping_pong_side = "sell" if fill.side == "buy" else "buy"
                if self.config.filled_order_delay > 0:
                    self._quote_after = self.context.clock() + self.config.filled_order_delay
            # Fill callbacks trigger a fresh account read; they never release tracked orders.
            self._fill_generation += 1
            self._needs_refresh = True

    def _must_replace(self, book: Book) -> bool:
        mid = self._quote_mid(book)
        if mid is None:
            return True
        return any(self._order_must_go(order, mid) for order in self._active.values())

    def _order_must_go(self, order: OrderRequest, mid: Decimal) -> bool:
        spread = (mid - order.price if order.side == "buy" else order.price - mid) / mid
        if spread < self.config.minimum_spread:
            return True
        if order.side == "buy" and self.config.price_ceiling is not None:
            if mid >= self.config.price_ceiling:
                return True
        if order.side == "sell" and self.config.price_floor is not None:
            if mid <= self.config.price_floor:
                return True
        return False

    def _age_expired(self, now: float) -> bool:
        return (
            self._active_since is not None and now - self._active_since >= self.config.max_order_age
        )

    def _within_tolerance(self, desired: tuple[OrderRequest, ...]) -> bool:
        tolerance = self.config.price_refresh_tolerance
        if tolerance is None or len(desired) != len(self._active):
            return False

        def key(order: OrderRequest) -> tuple[str, str, Decimal, Decimal]:
            return order.symbol, order.side, order.price, order.quantity

        for old, new in zip(
            sorted(self._active.values(), key=key), sorted(desired, key=key), strict=True
        ):
            if (old.symbol, old.side, old.quantity, old.reduce_only, old.time_in_force) != (
                new.symbol,
                new.side,
                new.quantity,
                new.reduce_only,
                new.time_in_force,
            ) or abs(new.price - old.price) * 100 > old.price * tolerance:
                return False
        return True

    async def _cancel_active(self) -> None:
        await self._cancel(list(self._active))
        self._ping_fills.clear()
        self._active_since = None

    async def _cancel(self, order_ids: list[str]) -> None:
        requests = [CancelRequest(self.context.symbol, order_id=oid) for oid in order_ids]
        if not requests:
            return
        self._record_actions(len(requests))
        results = await self.context.client.cancel_orders(requests)
        if len(results) != len(requests):
            raise RuntimeError("cancellation outcome unavailable")
        for request, result in zip(requests, results, strict=True):
            if result.status not in ("cancelled", "already_closed"):
                raise RuntimeError("cancellation not confirmed")
            if result.order_id not in (None, request.order_id):
                raise RuntimeError("cancellation identity mismatch")
            assert request.order_id is not None
            if result.client_order_id not in (
                None,
                self._active[request.order_id].client_order_id,
            ):
                raise RuntimeError("cancellation identity mismatch")
        for oid in order_ids:
            self._active.pop(oid, None)
            self._placed_at.pop(oid, None)

    def _record_actions(self, count: int) -> None:
        self._action_times.extend([self.context.clock()] * count)

    def _action_budget(self) -> float:
        """Order entries this refresh may still send under max_quote_actions_per_minute."""
        cap = self.config.max_quote_actions_per_minute
        now = self.context.clock()
        self._action_times = [t for t in self._action_times if now - t < 60]
        return math.inf if cap is None else cap - len(self._action_times)

    def _throttle(self, error: RateLimited) -> None:
        """Back off after a rate or budget refusal, keeping resting quotes as they are."""
        self._rate_backoff = min(max(self._rate_backoff * 2, RATE_BACKOFF_SECONDS), 300.0)
        self._throttled_until = self.context.clock() + max(self._rate_backoff, error.retry_after)

    async def on_tick(self, now: float | None = None) -> None:
        async with self._lock:
            if self.blocked:
                return
            now = self.context.clock() if now is None else now
            if not math.isfinite(now) or now < self._last_tick:
                raise ValueError("tick time must be finite and monotonic")
            self._last_tick = now
            clock_start = self.context.clock()
            book = self.book
            if book is not None and (
                self.config.price_floor is not None or self.config.price_ceiling is not None
            ):
                mid = self._quote_mid(book)
                assert mid is not None
                band = (
                    self.config.price_floor is not None and mid <= self.config.price_floor,
                    self.config.price_ceiling is not None and mid >= self.config.price_ceiling,
                )
                if self._last_band is not None and band != self._last_band:
                    self._needs_refresh = True
                self._last_band = band
            due = (
                self._last_refresh is None
                or now - self._last_refresh >= self.config.order_refresh_time
            )
            if (
                book is not None
                and not due
                and not self._needs_refresh
                and not self._age_expired(now)
                and not self._must_replace(book)
            ):
                return
            try:
                if self.context.clock() < self._throttled_until:
                    return
                if self.context.clock() < self._quote_after:
                    await self._cancel_active()
                    return
                if self.config.quote_refresh == "changed_levels":
                    await self._refresh_changed_levels(now)
                    return
                if self._action_budget() < len(self._active):
                    self._wait_for_action_budget()
                    return
                if (
                    book is not None
                    and self._active
                    and self.config.price_refresh_tolerance is not None
                    and not self._needs_refresh
                    and not self._age_expired(now)
                    and not self._must_replace(book)
                ):
                    account = await self.context.client.account()
                    if self.blocked:
                        return
                    elapsed = self.context.clock() - clock_start
                    if not math.isfinite(elapsed) or elapsed < 0:
                        raise ValueError("clock must be finite and monotonic")
                    book = self.book
                    if (
                        book is not None
                        and not self._needs_refresh
                        and not self._age_expired(now + elapsed)
                        and not self._must_replace(book)
                        and self._within_tolerance(self.quotes(book, account))
                    ):
                        self._last_refresh = now
                        return
                await self._cancel_active()
                if self.blocked:
                    return
                account = await self.context.client.account()
                if self.blocked:
                    return
                if self.context.clock() < self._quote_after:
                    return
                # Cancellation/account awaits may have received a newer book or fills.
                book = self.book
                if book is None:
                    return
                generation = self._fill_generation
                targets = list(self.quotes(book, account))
                budget = max(int(min(self._action_budget(), len(targets))), 0)
                await self._place(targets[:budget], now, generation)
            except RateLimited as error:
                self._throttle(error)
            except BaseException:
                self.blocked = True
                raise

    async def _refresh_changed_levels(self, now: float) -> None:
        """Keep quotes still close to their target level; cancel and place only the rest."""
        account = await self.context.client.account()
        if self.blocked:
            return
        book = self.book
        if book is None:
            return
        mid = self._quote_mid(book)
        assert mid is not None
        generation = self._fill_generation
        wanted = list(self.quotes(book, account))
        tolerance = self.config.price_refresh_tolerance
        stale = []
        for oid, order in self._active.items():
            match = None
            age = now - self._placed_at.get(oid, now)
            if age < self.config.max_order_age and not self._order_must_go(order, mid):
                match = next(
                    (
                        target
                        for target in wanted
                        if (target.side, target.quantity) == (order.side, order.quantity)
                        and (
                            target.price == order.price
                            if tolerance is None
                            else abs(target.price - order.price) * 100 <= order.price * tolerance
                        )
                    ),
                    None,
                )
            if match is None:
                stale.append(oid)
            else:
                wanted.remove(match)
        budget = self._action_budget()
        if len(stale) > budget:
            # Cancels come first; place nothing until every moved quote is gone.
            await self._cancel(stale[: max(int(budget), 0)])
            self._wait_for_action_budget()
            return
        await self._cancel(stale)
        if self.blocked:
            return
        await self._place(
            wanted[: max(int(min(self._action_budget(), len(wanted))), 0)], now, generation
        )

    def _wait_for_action_budget(self) -> None:
        if self._action_times:
            self._throttled_until = max(self._throttled_until, self._action_times[0] + 60)

    async def _place(self, targets: list[OrderRequest], now: float, generation: int) -> None:
        orders = [
            OrderRequest(
                order.symbol,
                order.side,
                order.quantity,
                order.price,
                client_order_id=bot_client_order_id(),
                time_in_force="Alo",
            )
            for order in targets
        ]
        if orders:
            self._record_actions(len(orders))
            results = await self.context.client.place_orders(orders)
            if len(results) != len(orders):
                raise RuntimeError("order outcome unavailable")
        else:
            results = []
        immediate_fill = False
        for order, result in zip(orders, results, strict=True):
            if result.client_order_id not in (None, order.client_order_id):
                raise RuntimeError("order identity mismatch")
            if result.status == "resting" and result.order_id:
                if result.order_id in self._active:
                    raise RuntimeError("duplicate resting order identity")
                self._active[result.order_id] = order
                self._placed_at[result.order_id] = now
            elif result.status == "filled":
                if self.config.ping_pong_enabled:
                    raise RiskViolation("ping_pong_fill_evidence")
                immediate_fill = True
                if self.config.filled_order_delay > 0:
                    self._quote_after = self.context.clock() + self.config.filled_order_delay
            elif result.status != "rejected":
                raise RuntimeError("order outcome unknown")
        self._active_since = min(self._placed_at.values(), default=None)
        self._last_refresh = now
        self._needs_refresh = immediate_fill or generation != self._fill_generation
        self._rate_backoff = 0.0

    async def stop(self) -> None:
        self.blocked = True
        async with self._lock:
            await self.context.client.cancel_all(self.context.symbol)
            self._active.clear()
            self._placed_at.clear()
            self._ping_fills.clear()
            self._active_since = None
