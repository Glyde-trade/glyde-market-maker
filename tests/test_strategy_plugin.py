from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal as D
from types import ModuleType

import pytest

from glyde_mm.config import load_config
from glyde_mm.models import Book, Fill, Level, OrderRequest, OrderResult
from glyde_mm.paper import PaperExchange
from glyde_mm.risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation
from glyde_mm.strategies import StrategyContext, create_strategy
from glyde_mm.strategies.factory import validate_strategy_config
from glyde_mm.strategies.plugin import (
    PluginContext,
    Quote,
    StrategyPlugin,
    parse_parameters,
)

EXAMPLE = "plugin:glyde_mm.strategies.example_plugin:SymmetricQuotes"


def book():
    return Book("BTC", (Level(D(99), D(10)),), (Level(D(101), D(10)),), 0, 0)


class FixedClockPaper(PaperExchange):
    async def account(self):
        return replace(await super().account(), received_at=0)


async def setup(name=EXAMPLE, values=None):
    paper = FixedClockPaper(D(1000))
    await paper.on_book(book())
    guard = RiskGuard(
        RiskLimits(max_position=D(1), max_order_notional=D(100)),
        clock=lambda: 0,
        wall_clock=lambda: 0,
    )
    guard.update_book(book())
    guard.update_account(replace(await paper.account(), received_at=0))
    managed = RiskManagedClient(paper, guard)
    context = StrategyContext(managed, "BTC", D("0.01"), D("0.001"), clock=lambda: 0)
    strategy = create_strategy(name, context, values or {"parameters": {"amount": "0.1"}})
    return paper, guard, context, strategy


@pytest.fixture
def plugin_module(monkeypatch):
    import sys

    module = ModuleType("local_test_strategy")
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def publish(cls):
        module.Policy = cls
        return f"plugin:{module.__name__}:Policy"

    return publish


@dataclass(frozen=True)
class Parameters:
    amount: D = D("0.1")
    levels: int = 1
    enabled: bool = True
    name: str = "example"
    pause: float = 1.0
    floor: D | None = None


def test_parameters_are_typed_and_defaults_checked():
    value = parse_parameters(Parameters, {"amount": "0.2", "pause": 2, "floor": "10"})
    assert value == Parameters(amount=D("0.2"), pause=2.0, floor=D(10))
    assert type(value.pause) is float


@pytest.mark.parametrize(
    "values",
    [
        {"unknown": 1},
        {"amount": True},
        {"amount": "NaN"},
        {"amount": "Infinity"},
        {"levels": True},
        {"levels": "2"},
        {"enabled": "false"},
        {"name": 3},
        {"pause": True},
        {"pause": float("inf")},
        {"floor": []},
    ],
)
def test_bad_parameter_types_refuse(values):
    with pytest.raises(ValueError):
        parse_parameters(Parameters, values)


def test_schema_rejects_mutable_configs_unsupported_fields_and_wrong_defaults():
    @dataclass
    class Mutable:
        amount: D = D(1)

    @dataclass(frozen=True)
    class Collection:
        rows: list[str] = ()

    @dataclass(frozen=True)
    class BadDefault:
        amount: D = 1

    for schema in (Mutable, Collection, BadDefault):
        with pytest.raises(ValueError):
            parse_parameters(schema, {})


@pytest.mark.parametrize(
    "name,values",
    [
        ("plugin:../unsafe:Class", {}),
        ("plugin:module.Class", {}),
        (EXAMPLE, {"client": "raw"}),
        (EXAMPLE, {"order_refresh_time": True}),
        (EXAMPLE, {"max_order_age": 0}),
        (EXAMPLE, {"parameters": {"spread": "1"}}),
    ],
)
def test_invalid_plugin_definition_refuses_before_execution(name, values):
    with pytest.raises(ValueError):
        validate_strategy_config(name, values)


async def test_example_uses_host_ids_passive_precision_cancel_refresh_and_stop():
    paper, _, _, strategy = await setup()
    await strategy.on_book(book())
    await strategy.on_tick(0)
    original = await paper.open_orders()
    assert [(o.side, o.price, o.quantity) for o in original] == [
        ("buy", D("99.80"), D("0.1")),
        ("sell", D("100.20"), D("0.1")),
    ]
    assert all(o.client_order_id.startswith("0x") for o in original)
    await strategy.on_tick(29)
    assert await paper.open_orders() == original
    await strategy.on_tick(30)
    assert {o.order_id for o in await paper.open_orders()}.isdisjoint(o.order_id for o in original)
    await strategy.stop()
    assert await paper.open_orders() == []


async def test_plugin_gets_no_client_or_credentials_and_cannot_change_account(plugin_module):
    events = []

    class Policy(StrategyPlugin[Parameters]):
        config_type = Parameters

        async def on_start(self, context):
            assert type(context) is PluginContext
            assert set(vars(context)) == {"symbol", "price_tick", "quantity_step", "min_notional"}
            events.append("start")

        async def on_book(self, snapshot):
            events.append("book")

        async def on_tick(self, now):
            events.append("tick")

        async def on_fill(self, fill):
            events.append("fill")

        def quotes(self, context, snapshot, account):
            account.positions["BTC"] = D(999)
            events.append("quotes")
            return [Quote("buy", self.config.amount, D(99))]

        async def on_stop(self):
            events.append("stop")

    paper, guard, _, strategy = await setup(plugin_module(Policy))
    await strategy.on_book(book())
    await strategy.on_tick(0)
    assert guard.account_state.positions == {}
    assert (await paper.account()).positions == {}
    order = (await paper.open_orders())[0]
    await strategy.on_fill(Fill("BTC", "buy", D("0.01"), D(99), order.order_id))
    await strategy.on_tick(1)
    await strategy.stop()
    assert events == ["start", "book", "tick", "quotes", "fill", "tick", "quotes", "stop"]


async def test_plugin_cannot_select_raw_client():
    _, _, context, _ = await setup()
    with pytest.raises(ValueError, match="RiskManagedClient"):
        create_strategy(EXAMPLE, replace(context, client=PaperExchange(D(1000))), {})


async def test_oversized_plugin_is_refused_by_real_risk_boundary():
    paper, guard, _, strategy = await setup(values={"parameters": {"amount": "2"}})
    await strategy.on_book(book())
    with pytest.raises(RiskViolation, match="order_notional|position"):
        await strategy.on_tick(0)
    assert await paper.open_orders() == [] and paper.fills == []
    assert guard.halted and strategy.blocked
    await strategy.stop()


async def test_plugin_empty_quotes_pull_without_direct_cancel_access(plugin_module):
    class Policy(StrategyPlugin[Parameters]):
        config_type = Parameters
        enabled = True

        def quotes(self, context, snapshot, account):
            return [Quote("buy", D("0.1"), D(99))] if self.enabled else []

    paper, _, _, strategy = await setup(plugin_module(Policy))
    await strategy.on_book(book())
    await strategy.on_tick(0)
    strategy._plugin.enabled = False
    await strategy.on_tick(30)
    assert await paper.open_orders() == []


@pytest.mark.parametrize("invalid", [[OrderRequest("ETH", "buy", D(1), D(1))], [None] * 101])
async def test_invalid_intents_fail_closed(plugin_module, invalid):
    class Policy(StrategyPlugin[Parameters]):
        config_type = Parameters

        def quotes(self, context, snapshot, account):
            return invalid

    paper, _, _, strategy = await setup(plugin_module(Policy))
    await strategy.on_book(book())
    with pytest.raises(ValueError):
        await strategy.on_tick(0)
    assert strategy.blocked and await paper.open_orders() == []
    await strategy.stop()


async def test_unknown_outcome_freezes_and_does_not_call_plugin_again():
    paper, guard, _, strategy = await setup()
    calls = 0

    async def unknown(orders):
        nonlocal calls
        calls += 1
        return [OrderResult("unknown", client_order_id=o.client_order_id) for o in orders]

    paper.place_orders = unknown
    await strategy.on_book(book())
    with pytest.raises(RiskViolation, match="outcome_unknown"):
        await strategy.on_tick(0)
    await strategy.on_tick(30)
    assert calls == 1 and strategy.blocked and guard.halted == "order_outcome_unknown"
    with pytest.raises(RiskViolation, match="cancel_outcome_unknown"):
        await strategy.stop()
    assert guard.pending  # Unproven cancellation must not release ambiguous exposure.


async def test_hook_error_cleanup_precedes_plugin_stop(plugin_module):
    checks = []

    class Policy(StrategyPlugin[Parameters]):
        config_type = Parameters

        async def on_tick(self, now):
            if now > 0:
                raise RuntimeError("plugin failed")

        def quotes(self, context, snapshot, account):
            return [Quote("buy", D("0.1"), D(99))]

        async def on_stop(self):
            checks.append(await paper.open_orders())
            raise RuntimeError("stop hook failed")

    paper, _, _, strategy = await setup(plugin_module(Policy))
    await strategy.on_book(book())
    await strategy.on_tick(0)
    with pytest.raises(RuntimeError, match="plugin failed"):
        await strategy.on_tick(1)
    assert strategy.blocked
    with pytest.raises(RuntimeError, match="stop hook failed"):
        await strategy.stop()
    assert checks == [[]]


async def test_hook_timeout_blocks_no_dispatch(plugin_module):
    class Policy(StrategyPlugin[Parameters]):
        config_type = Parameters

        async def on_tick(self, now):
            raise TimeoutError("user hook")

        def quotes(self, context, snapshot, account):
            pytest.fail("failed hook must not quote")

    paper, _, _, strategy = await setup(plugin_module(Policy))
    await strategy.on_book(book())
    with pytest.raises(TimeoutError):
        await strategy.on_tick(0)
    assert strategy.blocked and await paper.open_orders() == []
    await strategy.stop()


def test_example_yaml_loads_typed_config():
    config = load_config("examples/plugin-paper.yaml")
    selected = validate_strategy_config(config.strategy, config.strategy_config)
    assert config.mode == "paper" and selected.parameters.amount == D("0.0002")


async def test_cancelled_hook_blocks_and_host_cleanup_still_runs(plugin_module):
    started = asyncio.Event()

    class Policy(StrategyPlugin[Parameters]):
        config_type = Parameters

        async def on_tick(self, now):
            started.set()
            await asyncio.Event().wait()

        def quotes(self, context, snapshot, account):
            pytest.fail("cancelled hook must not quote")

    paper, _, _, strategy = await setup(plugin_module(Policy))
    await strategy.on_book(book())
    task = asyncio.create_task(strategy.on_tick(0))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert strategy.blocked and await paper.open_orders() == []
    await strategy.stop()


async def test_plugin_runs_paper_runner_with_shared_cleanup(tmp_path):
    from test_runner import Paper, Source, config

    from glyde_mm.runner import run

    stop, placed = asyncio.Event(), asyncio.Event()
    selected = replace(
        config(), strategy=EXAMPLE, strategy_config={"parameters": {"amount": "0.01"}}
    )
    source = Source(selected, stop=stop, placed=placed)
    paper = Paper(selected.paper_balance, placed=placed, stop=stop)
    await asyncio.wait_for(
        run(
            selected,
            state_directory=tmp_path,
            client_factory=lambda _: source,
            paper_factory=lambda _: paper,
            stop_event=stop,
            report=lambda _: None,
        ),
        2,
    )
    assert len(paper.submissions) == 2 and source.live_orders == []
    assert await paper.open_orders() == []
    assert all(o.time_in_force == "Alo" for o in paper.submissions)
