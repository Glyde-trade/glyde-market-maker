from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest

from glyde_mm.config import Config, MarketConfig, RiskConfig, load_config
from glyde_mm.models import Account, Book, Level, OpenOrder, OrderRequest
from glyde_mm.risk import RiskGuard, RiskLimits, RiskViolation


def test_markets_explicit_caps_and_independent_strategy_settings(tmp_path):
    path = tmp_path / "markets.yaml"
    path.write_text("""mode: paper
strategy_config: {order_amount: '0.01', bid_spread: '0.02'}
risk: {max_total_notional: '50', max_orders_per_minute: 7}
markets:
  - symbol: BTC
    max_position: '0.1'
  - symbol: ETH
    max_position: '1'
    strategy_config: {order_amount: '0.2'}
""")
    config = load_config(path)
    btc, eth = config.market_configs()
    assert config.symbols == ("BTC", "ETH")
    assert btc.risk.max_position == D(".1") and eth.risk.max_position == D("1")
    assert (
        btc.strategy_config["order_amount"] == ".01"
        or btc.strategy_config["order_amount"] == "0.01"
    )
    assert eth.strategy_config == {"order_amount": "0.2", "bid_spread": "0.02"}
    assert btc.risk.max_orders_per_minute == eth.risk.max_orders_per_minute == 7


@pytest.mark.parametrize(
    "source",
    [
        "markets: []",
        "markets: [{symbol: BTC, max_position: 1}]",
        "symbol: ETH\nmarkets: [{symbol: BTC, max_position: 1}]\nrisk: {max_total_notional: 50}",
        "markets: [{symbol: BTC, max_position: 1}, {symbol: BTC, max_position: 2}]\nrisk: {max_total_notional: 50}",
        "markets: [{symbol: BTC}]\nrisk: {max_total_notional: 50}",
        "markets: [{symbol: BTC, max_position: 0}]\nrisk: {max_total_notional: 50}",
        "markets: [{symbol: BTC, max_position: 1, strategy_config: false}]\nrisk: {max_total_notional: 50}",
        "markets: [{symbol: BTC, max_position: 1}]\nrisk: {max_total_notional: -1}",
    ],
)
def test_invalid_multi_config_refuses(tmp_path, source):
    path = tmp_path / "bad.yaml"
    path.write_text(source)
    with pytest.raises(ValueError):
        load_config(path)


def book(symbol, bid="99", ask="101"):
    return Book(symbol, (Level(D(bid), D(10)),), (Level(D(ask), D(10)),), 1_700_000_000_000, 100)


def guard(*, total="50", positions=None, state_path=None, rate=60):
    g = RiskGuard(
        RiskLimits(
            max_position=D(1),
            max_order_notional=D(100),
            max_total_notional=D(total),
            max_orders_per_minute=rate,
        ),
        clock=lambda: 100,
        wall_clock=lambda: 1_700_000_000,
        state_path=state_path,
        position_limits={"BTC": D(".2"), "ETH": D("2")},
    )
    g.update_book(book("BTC"))
    g.update_book(book("ETH", "9", "11"))
    g.update_account(Account(D(100), positions or {}, received_at=100))
    return g


def test_gross_notional_does_not_net_opposite_assets_and_uses_pending():
    g = guard(total="30", positions={"BTC": D(".1"), "ETH": D("-1")})
    # 0.1 BTC * 101 + |-1 - 0.8| ETH * 11 = 29.9, not a net dollar position.
    g.reserve(OrderRequest("ETH", "sell", D(".8"), D(11), "first"))
    with pytest.raises(RiskViolation, match="max_total_notional"):
        g.validate_order("BTC", "buy", D(".01"), D(99))
    assert len(g.pending) == 1


def test_exact_total_boundary_and_external_open_order_dedup():
    g = guard(total="20.2")
    order = OrderRequest("BTC", "buy", D(".1"), D(99), "first")
    g.reserve(order)
    g.set_open_orders([OpenOrder("1", "BTC", "buy", D(".1"), D(99), "first")])
    g.validate_order("BTC", "buy", D(".1"), D(99))
    g.set_open_orders([OpenOrder("2", "ETH", "sell", D(".01"), D(11), "external")])
    with pytest.raises(RiskViolation, match="max_total_notional"):
        g.validate_order("BTC", "buy", D(".1"), D(99))


def test_per_market_position_caps_and_unknown_exposure_fail_closed():
    g = guard(total="1000")
    with pytest.raises(RiskViolation, match="max_position"):
        g.validate_order("BTC", "buy", D(".21"), D(99))
    g.validate_order("ETH", "buy", D("1.5"), D(9))
    g.update_account(Account(D(100), {"SOL": D(1)}, received_at=100))
    with pytest.raises(RiskViolation, match="stale_book"):
        g.validate_order("BTC", "buy", D(".01"), D(99))
    g.update_account(Account(D(100), {"ETH": D(1)}, received_at=100))
    g.books["ETH"] = replace(g.books["ETH"], received_at=90)
    with pytest.raises(RiskViolation, match="stale_book"):
        g.validate_order("BTC", "buy", D(".01"), D(99))


def test_daily_loss_and_attempt_budget_shared_across_markets_and_restart(tmp_path):
    path = tmp_path / "account.json"
    g = guard(state_path=path, rate=2)
    g.reserve(OrderRequest("BTC", "buy", D(".01"), D(99), "btc"))
    g.consume_order_rate(1)
    g.reserve(OrderRequest("ETH", "buy", D(".1"), D(9), "eth"))
    g.consume_order_rate(1)
    restored = guard(state_path=path, rate=2)
    with pytest.raises(RiskViolation, match="order_rate_limit"):
        restored.consume_order_rate(1)
    with pytest.raises(RiskViolation, match="daily_loss"):
        g.update_account(Account(D(94), {"BTC": D(".01"), "ETH": D(".1")}, received_at=100))
    with pytest.raises(RiskViolation, match="daily_loss"):
        g.validate_order("ETH", "buy", D(".1"), D(9))


def test_single_config_keeps_existing_default():
    config = Config()
    assert config.market_configs() == (config,)
    assert config.symbols == ("BTC",)
    with pytest.raises(ValueError, match="max_total_notional"):
        Config(markets=(MarketConfig("ETH", D(1)),), risk=RiskConfig())
