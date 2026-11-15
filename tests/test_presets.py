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
