"""Editable paper presets sized at a user-supplied reference price, never live authority."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

STRATEGIES = ("pure_market_making", "avellaneda_market_making", "grid", "inventory_skewed")
TRADEOFFS = {
    "conservative": "Wider quotes and slower refresh: fewer fills, less quote turnover.",
    "balanced": "Moderate spacing and refresh: a middle ground for paper experiments.",
    "active": "Tighter quotes and faster refresh: more turnover and inventory changes.",
}


def strategy_preset(
    strategy: str,
    preset: str,
    reference_price: Decimal,
    max_order_notional: Decimal,
    max_position: Decimal,
    *,
    reference_source: str = "glyde",
    ping_pong_enabled: bool = False,
) -> dict:
    if type(ping_pong_enabled) is not bool or (
        ping_pong_enabled and strategy != "pure_market_making"
    ):
        raise ValueError("ping-pong is a boolean PMM-only option")
    if strategy not in STRATEGIES or preset not in TRADEOFFS:
        raise ValueError("Choose a listed strategy and preset.")
    if any(
        not value.is_finite() or value <= 0
        for value in (reference_price, max_order_notional, max_position)
    ):
        raise ValueError("Reference price and risk limits must be positive finite decimals.")
    if reference_source not in {"glyde", "hyperliquid"} or (
        reference_source == "hyperliquid" and strategy != "pure_market_making"
    ):
        raise ValueError("external reference is supported by pure_market_making only")
    index = tuple(TRADEOFFS).index(preset)
    levels = (1, 2, 3)[index]
    spread = ("0.005", "0.002", "0.001")[index]
    refresh = (60, 30, 10)[index]
    # Budget every level on one side and PMM's maximum twofold skew, with 20% headroom.
    bound = reference_price * Decimal("1.2")
    amount = min(
        max_order_notional * Decimal("0.8") / (2 * bound),
        max_position * Decimal("0.8") / (2 * levels),
    )
    if strategy == "grid":
        levels = (3, 5, 7)[index]
        amount = min(amount, max_position * Decimal("0.8") / levels)
    amount = amount.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    if amount <= 0:
        raise ValueError("These caps are too small for a preset; no configuration was written.")
    values = {"order_amount": str(amount), "order_refresh_time": refresh}
    if strategy == "grid":
        values.update(
            lower_price=str(reference_price * Decimal("0.9")),
            upper_price=str(reference_price * Decimal("1.1")),
            levels=levels,
            minimum_spread=spread,
        )
    else:
        values.update(
            order_levels=levels,
            order_level_spread=spread,
            order_level_amount="0",
            minimum_spread=spread,
            inventory_target_position="0",
        )
        if strategy == "pure_market_making":
            values.update(
                bid_spread=spread,
                ask_spread=spread,
                inventory_skew_enabled=True,
                inventory_range_multiplier="1",
                hanging_orders_enabled=False,
                ping_pong_enabled=ping_pong_enabled,
                price_floor=None,
                price_ceiling=None,
            )
        else:
            values.update(
                risk_factor=("2", "1", "0.5")[index],
                liquidity_kappa="1",
                time_left_fraction="1",
                volatility_samples=30,
                sample_interval_ms=1000,
            )
    if strategy == "inventory_skewed":
        values["order_amount_shape_factor"] = ("2", "1", "0.5")[index]
    if reference_source == "hyperliquid":
        values.update(
            reference_source="hyperliquid",
            reference_skew_bps="0",
            max_reference_deviation_bps=("25", "50", "100")[index],
            reference_stale_seconds="3",
        )
    return values
