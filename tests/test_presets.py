from decimal import Decimal as D

import pytest

from glyde_mm.presets import STRATEGIES, TRADEOFFS, strategy_preset
from glyde_mm.strategies import StrategyContext, create_strategy


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("preset", TRADEOFFS)
def test_all_presets_construct_and_share_risk_budgets(strategy, preset):
    values = strategy_preset(strategy, preset, D(100000), D(25), D("0.001"))
    create_strategy(strategy, StrategyContext(None, "BTC", D(1), D("0.00001")), values)
    amount = D(values["order_amount"])
    levels = values.get("levels", values.get("order_levels"))
    skew = 2 if strategy == "pure_market_making" else 1
    assert amount * skew * levels <= D("0.001")
    assert amount * skew * D(120000) <= D(25)
    assert values["order_refresh_time"] > 0
    if strategy == "grid":
        assert D(values["lower_price"]) < D(100000) < D(values["upper_price"])


@pytest.mark.parametrize("reference", [D(0), D(-1), D("NaN"), D("Infinity")])
def test_invalid_reference_refuses(reference):
    with pytest.raises(ValueError):
        strategy_preset("grid", "active", reference, D(25), D("0.001"))


def test_too_small_budget_refuses_instead_of_writing_zero_amount():
    with pytest.raises(ValueError, match="too small"):
        strategy_preset("pure_market_making", "conservative", D("1e20"), D(25), D("0.001"))


@pytest.mark.parametrize("preset", TRADEOFFS)
def test_ping_pong_opt_in_preset_preserves_caps_and_explicit_optional_bands(preset):
    values = strategy_preset(
        "pure_market_making", preset, D(100000), D(25), D(".001"), ping_pong_enabled=True
    )
    s = create_strategy(
        "pure_market_making", StrategyContext(None, "BTC", D(1), D(".00001")), values
    )
    assert s.config.ping_pong_enabled is True
    assert values["price_floor"] is values["price_ceiling"] is None
    amount = D(values["order_amount"])
    assert amount * 2 * values["order_levels"] <= D(".001")
    assert amount * 2 * D(120000) <= D(25)
    assert not strategy_preset("pure_market_making", preset, D(100000), D(25), D(".001"))[
        "ping_pong_enabled"
    ]
