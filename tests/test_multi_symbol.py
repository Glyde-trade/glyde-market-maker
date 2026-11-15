from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest
from market_receipt import approved_market

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
    assert btc.strategy_config["order_amount"] == "0.01"
    assert eth.strategy_config == {"order_amount": "0.2", "bid_spread": "0.02"}
    assert btc.risk.max_orders_per_minute == eth.risk.max_orders_per_minute == 7


@pytest.mark.parametrize(
    "source",
    [
        "markets: []",
        "markets: [{symbol: BTC, max_position: 1}]",
        "symbol: ETH\nmarkets: [{symbol: BTC, max_position: 1}]\nrisk: {max_total_notional: 50}",
        "markets: [{symbol: BTC, max_position: 1}, {symbol: BTC, max_position: 2}]"
        "\nrisk: {max_total_notional: 50}",
        "markets: [{symbol: BTC}]\nrisk: {max_total_notional: 50}",
        "markets: [{symbol: BTC, max_position: 0}]\nrisk: {max_total_notional: 50}",
        "markets: [{symbol: BTC, max_position: 1, strategy_config: false}]"
        "\nrisk: {max_total_notional: 50}",
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
    import asyncio

    asyncio.run(g.flush_state())
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


async def paper_session(tmp_path, *, aggregate="50", rate=60, stale=False):
    import asyncio
    import time

    from glyde_mm.paper import PaperExchange
    from glyde_mm.runner import run
    from glyde_mm.strategies import create_strategy

    stop, placed = asyncio.Event(), asyncio.Event()
    wire_time = time.time_ns() // 1_000_000
    selected = Config(
        markets=(
            MarketConfig("BTC", D(".1")),
            MarketConfig("ETH", D("1"), {"order_amount": "0.1"}),
        ),
        strategy_config={"order_amount": "0.01", "bid_spread": "0.02", "ask_spread": "0.02"},
        risk=RiskConfig(
            max_order_notional=D(25), max_total_notional=D(aggregate), max_orders_per_minute=rate
        ),
    )
    instances, clients, lines = {}, [], []

    class Source:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def meta(self):
            return {"universe": [{"name": s, "szDecimals": 2} for s in selected.symbols]}

        async def account(self):
            pytest.fail("paper cannot read a private account")

        async def stream(self, subscriptions):
            assert subscriptions == [
                {"type": "l2Book", "coin": "BTC"},
                {"type": "l2Book", "coin": "ETH"},
                {"type": "trades", "coin": "BTC"},
                {"type": "trades", "coin": "ETH"},
            ]
            for symbol, bid, ask in [("ETH", "9", "11"), ("BTC", "99", "101")]:
                yield {
                    "channel": "l2Book",
                    "data": {
                        "coin": symbol,
                        "time": wire_time - (60000 if stale and symbol == "ETH" else 0),
                        "levels": [[{"px": bid, "sz": "2"}], [{"px": ask, "sz": "2"}]],
                    },
                }
            await placed.wait()
            yield {
                "channel": "trades",
                "data": [
                    {
                        "coin": "ETH",
                        "side": "A",
                        "px": "9",
                        "sz": "0.1",
                        "time": wire_time + 1,
                        "tid": 1,
                    }
                ],
            }
            await asyncio.Event().wait()

    class Paper(PaperExchange):
        def __init__(self):
            super().__init__(selected.paper_balance, clock_ms=lambda: wire_time)
            self.batches = []
            self.cancelled = []

        async def place_orders(self, orders):
            self.batches.append(list(orders))
            results = await super().place_orders(orders)
            if len(self.batches) == 2:
                placed.set()
            return results

        async def cancel_all(self, symbol=None):
            self.cancelled.append(symbol)
            await super().cancel_all(symbol)

    paper = Paper()

    def factory(name, context, values):
        instance = create_strategy(name, context, values)
        original = instance.on_fill
        instance.observed_fills = []

        async def on_fill(fill):
            instance.observed_fills.append(fill)
            await original(fill)
            stop.set()

        instance.on_fill = on_fill
        instances[context.symbol] = instance
        clients.append(context.client)
        return instance

    error = None
    try:
        await asyncio.wait_for(
            run(
                selected,
                state_directory=tmp_path,
                client_factory=lambda _: Source(),
                paper_factory=lambda _: paper,
                strategy_factory=factory,
                stop_event=stop,
                report=lines.append,
            ),
            2,
        )
    except RiskViolation as caught:
        error = caught
    return selected, paper, instances, clients, lines, error


async def test_real_paper_two_strategies_one_account_tape_fill_and_scoped_cleanup(tmp_path):
    import json

    from glyde_mm.runner import risk_state_path

    selected, paper, instances, clients, lines, error = await paper_session(tmp_path)
    assert error is None
    assert set(instances) == {"BTC", "ETH"} and instances["BTC"] is not instances["ETH"]
    assert clients[0] is clients[1]
    assert [[o.symbol for o in batch] for batch in paper.batches] == [["BTC"] * 2, ["ETH"] * 2]
    assert not instances["BTC"].observed_fills
    assert len(instances["ETH"].observed_fills) == 1
    assert (await paper.account()).positions == {"ETH": D(".1")}
    assert not await paper.open_orders()
    assert set(paper.cancelled) == {"BTC", "ETH"} and None not in paper.cancelled
    state = json.loads(risk_state_path(selected, tmp_path).read_text())
    assert state["equity_start"] == "10000" and len(state["order_attempts"]) == 4
    assert len([line for line in lines if line.startswith("PAPER SUMMARY")]) == 2


@pytest.mark.parametrize(
    ("kwargs", "reason", "batches"),
    [
        ({"aggregate": "1.5"}, "max_total_notional", 1),
        ({"rate": 3}, "order_rate_limit", 1),
        ({"stale": True}, "stale_book_source", 0),
    ],
)
async def test_shared_risk_stops_all_markets_before_other_market_dispatch(
    tmp_path, kwargs, reason, batches
):
    _, paper, _, _, _, error = await paper_session(tmp_path, **kwargs)
    assert str(error) == reason
    assert len(paper.batches) == batches
    assert not await paper.open_orders()
    assert set(paper.cancelled) == {"BTC", "ETH"}


async def test_multi_preflight_checks_second_market_without_cancel(tmp_path):
    from glyde_mm.preflight import LivePreflightRefused, check_live_exposure

    class Source:
        async def account(self):
            return Account(D(1000), {"ETH": D(1)})

        async def open_orders(self):
            return []

    with pytest.raises(LivePreflightRefused):
        await check_live_exposure(Source(), ("BTC", "ETH"))
    await check_live_exposure(Source(), ("BTC", "ETH"), allow_existing_exposure=True)


async def test_stop_sweeps_all_selected_symbols_not_unrelated_market(tmp_path):
    from glyde_mm.shutdown import stop_bot

    selected = Config(
        mode="live",
        markets=(MarketConfig("BTC", D(1)), MarketConfig("ETH", D(1))),
        risk=RiskConfig(max_total_notional=D(100)),
    )

    class Source:
        def __init__(self):
            self.cancelled = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def cancel_all(self, symbol):
            self.cancelled.append(symbol)

        async def open_orders(self):
            return [OpenOrder("1", "SOL", "buy", D(1), D(10))]

    source = Source()
    result, _ = await stop_bot(selected, tmp_path, client_factory=lambda _: source)
    assert result["outcome"] == "stopped"
    assert source.cancelled == ["BTC", "ETH"]


async def test_record_replay_refuse_multi_before_any_io(tmp_path):
    from glyde_mm.book_history import record_books
    from glyde_mm.replay import replay

    selected = Config(
        markets=(MarketConfig("ETH", D(1)),), risk=RiskConfig(max_total_notional=D(100))
    )
    with pytest.raises(ValueError, match="single-symbol"):
        await record_books(selected, tmp_path / "missing", 1)
    with pytest.raises(ValueError, match="single-symbol"):
        await replay(selected, tmp_path / "missing", 1)


async def test_doctor_checks_both_markets_and_private_reads_once(tmp_path, monkeypatch):
    import time

    from glyde_mm import doctor

    path = tmp_path / "doctor.yaml"
    path.write_text("""mode: live
owner: '0x1111111111111111111111111111111111111111'
vault_address: '0x2222222222222222222222222222222222222222'
markets:
 - {symbol: BTC, max_position: '0.1'}
 - {symbol: ETH, max_position: '1'}
risk: {max_total_notional: '50'}
""")
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "must-not-load-or-print")
    monkeypatch.setenv("GLYDE_READ_API_KEY", "must-not-print")
    calls, lines = [], []

    class Source:
        def __init__(self, config):
            assert config.symbols == ("BTC", "ETH")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def meta(self):
            calls.append("meta")
            return {"universe": [{"name": s, "szDecimals": 2} for s in ("BTC", "ETH")]}

        async def book(self, symbol):
            calls.append(symbol)
            return replace(book(symbol), timestamp_ms=time.time_ns() // 1_000_000)

        async def account(self):
            calls.append("account")
            return Account(D(1000))

        async def open_orders(self):
            calls.append("orders")
            return []

    Source.public_clock = None
    monkeypatch.setattr(doctor, "OneApiClient", Source)
    assert await doctor.check(path, private=True, report=lines.append) == 0
    assert calls == ["meta", "BTC", "ETH", "account", "orders"]
    assert "must-not" not in "\n".join(lines)


async def test_shared_cleanup_attempts_second_market_after_first_failure():
    from glyde_mm.risk import RiskManagedClient

    calls = []

    class Source:
        async def cancel_all(self, symbol):
            calls.append(symbol)
            if symbol == "BTC":
                raise OSError("unavailable")

    managed = RiskManagedClient(Source(), guard(), cleanup_symbols=("BTC", "ETH"))
    with pytest.raises(RiskViolation, match="cancel_outcome_unknown"):
        await managed.close()
    assert calls == ["BTC", "ETH"]


def test_live_cli_requires_full_ordered_market_confirmation(tmp_path, monkeypatch, capsys):
    from glyde_mm import cli

    path = tmp_path / "live.yaml"
    path.write_text("""mode: live
markets:
 - {symbol: BTC, max_position: '0.1'}
 - {symbol: ETH, max_position: '1'}
risk: {max_total_notional: '50'}
""")
    calls = []

    async def run(selected, directory, **kwargs):
        calls.append(selected.symbols)

    monkeypatch.setattr(cli, "run_with_signals", run)
    monkeypatch.setattr("builtins.input", lambda _: "BTC")
    assert cli.main(["run", str(path)]) == 1 and not calls
    monkeypatch.setattr("builtins.input", lambda _: "BTC,ETH")
    approved_market(load_config(path), tmp_path / ".glyde-mm")
    assert cli.main(["run", str(path)]) == 0
    assert calls == [("BTC", "ETH")]
    assert "max_total_notional=50" in capsys.readouterr().out


def test_per_market_strategy_override_uses_shared_defaults(tmp_path):
    from glyde_mm.strategies import StrategyContext, create_strategy
    from glyde_mm.strategies.grid import GridStrategy

    path = tmp_path / "different-strategies.yaml"
    path.write_text("""mode: paper
strategy_config: {order_amount: '0.01', order_refresh_time: 2}
risk: {max_total_notional: '50'}
markets:
 - {symbol: BTC, max_position: '0.1', strategy_config: {bid_spread: '0.02'}}
 - symbol: ETH
   max_position: '1'
   strategy: grid
   strategy_config: {lower_price: '9', upper_price: '11', levels: 3, order_amount: '0.1'}
""")
    btc, eth = load_config(path).market_configs()
    assert btc.strategy == "pure_market_making" and eth.strategy == "grid"
    assert btc.strategy_config["bid_spread"] == "0.02"
    assert eth.strategy_config["order_refresh_time"] == 2
    strategy = create_strategy(
        eth.strategy, StrategyContext(None, "ETH", D("0.01"), D("0.001")), eth.strategy_config
    )
    assert isinstance(strategy, GridStrategy)
    with pytest.raises(ValueError, match="market strategy"):
        MarketConfig("ETH", D(1), strategy=False)


async def test_stale_exposed_market_blocks_shared_cap_without_discarding_position():
    from glyde_mm.models import OrderResult
    from glyde_mm.risk import RiskManagedClient, StaleMarket

    g = guard(total="100", positions={"ETH": D(1)})
    fresh = g.books["ETH"]
    g.books["ETH"] = replace(fresh, received_at=90)
    cancelled, sent = [], []

    class Source:
        async def account(self):
            return g.account_state

        async def open_orders(self):
            return []

        async def cancel_all(self, symbol):
            cancelled.append(symbol)

        async def place_orders(self, orders):
            sent.extend(orders)
            return [OrderResult("resting", "1", row.client_order_id) for row in orders]

    managed = RiskManagedClient(
        Source(), g, cleanup_symbols=("BTC", "ETH"), isolate_stale_markets=True
    )
    order = OrderRequest("BTC", "buy", D("0.01"), D(99))
    with pytest.raises(StaleMarket) as refusal:
        await managed.place_orders([order])
    assert refusal.value.symbol == "ETH"
    assert cancelled == ["ETH"] and not sent and g.halted is None
    assert g.account_state.positions == {"ETH": D(1)}
    g.books["ETH"] = fresh
    assert (await managed.place_orders([order]))[0].status == "resting"
    assert len(sent) == 1


async def test_unknown_stale_exposure_cannot_authorize_unconfigured_cancellation():
    from glyde_mm.risk import RiskManagedClient

    g = guard(total="100", positions={"SOL": D(1)})
    cancelled = []

    class Source:
        async def account(self):
            return g.account_state

        async def open_orders(self):
            return []

        async def cancel_all(self, symbol):
            cancelled.append(symbol)

        async def place_orders(self, orders):
            pytest.fail("unvalued exposure must refuse")

    managed = RiskManagedClient(
        Source(), g, cleanup_symbols=("BTC", "ETH"), isolate_stale_markets=True
    )
    with pytest.raises(RiskViolation, match="stale_book"):
        await managed.place_orders([OrderRequest("BTC", "buy", D("0.01"), D(99))])
    assert cancelled == ["BTC", "ETH"] and g.halted == "stale_book"


async def test_owner_refresh_waits_until_inflight_cancellation_is_resolved():
    import asyncio

    from glyde_mm.models import CancelRequest, CancelResult
    from glyde_mm.risk import RiskManagedClient

    g = guard()
    started, finish = asyncio.Event(), asyncio.Event()
    reads = []

    class Source:
        async def cancel_orders(self, requests):
            started.set()
            await finish.wait()
            return [
                CancelResult("cancelled", row.order_id, row.client_order_id) for row in requests
            ]

        async def account(self):
            reads.append("account")
            return g.account_state

        async def open_orders(self):
            return []

    managed = RiskManagedClient(Source(), g)
    cancelling = asyncio.create_task(managed.cancel_orders([CancelRequest("BTC", "1")]))
    await started.wait()
    refreshing = asyncio.create_task(managed.refresh_account())
    await asyncio.sleep(0)
    assert not refreshing.done() and not reads and g.halted is None
    finish.set()
    await cancelling
    await refreshing
    assert reads == ["account"] and g.halted is None and g._execution is None


@pytest.mark.parametrize("exposure", ["position", "pending", "open", "flat", "no_account"])
def test_recovery_dependency_requires_actual_exposure(exposure):
    g = guard()
    if exposure == "position":
        g.update_account(replace(g.account_state, positions={"ETH": D("0.1")}))
    elif exposure == "pending":
        g.pending["intent"] = OrderRequest("ETH", "buy", D("0.1"), D(9), "intent")
    elif exposure == "open":
        g.set_open_orders([OpenOrder("1", "ETH", "sell", D("0.1"), D(11))])
    elif exposure == "no_account":
        g.account_state = None
    assert g.has_market_exposure("ETH") == (exposure != "flat")
