from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal as D

import pytest

from glyde_mm.models import Account, Book, CancelResult, Fill, Level, OrderResult
from glyde_mm.strategies import (
    PureMarketMakingConfig,
    PureMarketMakingStrategy,
    StrategyContext,
)


def book(mid="100", timestamp=1):
    return Book(
        "BTC",
        (Level(D(mid) - 1, D(10)),),
        (Level(D(mid) + 1, D(10)),),
        timestamp,
        float(timestamp),
    )


class Client:
    def __init__(self):
        self.current = Account(D(10000))
        self.calls = []
        self.outcome = "resting"
        self.cancel_outcome = "cancelled"
        self.cancel_gate = None
        self.place_gate = None
        self.serial = 0
        self.on_account = None

    async def account(self):
        if self.on_account:
            await self.on_account()
        return self.current

    async def place_orders(self, orders):
        self.calls.append(("place", tuple(orders)))
        self.serial += 1
        if self.place_gate:
            await self.place_gate.wait()
        if self.outcome == "timeout":
            raise TimeoutError("accepted, response lost")
        return [
            OrderResult(self.outcome, f"{self.serial}-{i}", o.client_order_id)
            for i, o in enumerate(orders)
        ]

    async def cancel_orders(self, requests):
        self.calls.append(("cancel", tuple(requests)))
        if self.cancel_gate:
            await self.cancel_gate.wait()
        assert all(o.order_id and o.client_order_id is None for o in requests)
        return [CancelResult(self.cancel_outcome, r.order_id) for r in requests]

    async def cancel_all(self, symbol=None):
        self.calls.append(("all", symbol))


def setup(**options):
    client = Client()
    context = StrategyContext(client, "BTC", D("0.01"), D("0.001"))
    config = PureMarketMakingConfig(
        order_amount=D("1"), bid_spread=D("0.01"), ask_spread=D("0.02"), **options
    )
    return client, PureMarketMakingStrategy(context, config)


def test_spreads_levels_amount_steps_and_passive_rounding():
    client, s = setup(order_levels=3, order_level_spread=D("0.01"), order_level_amount=D(1))
    q = s.quotes(book(), client.current)
    assert [(o.side, o.price, o.quantity) for o in q] == [
        ("buy", D(99), D(1)),
        ("sell", D(102), D(1)),
        ("buy", D(98), D(2)),
        ("sell", D(103), D(2)),
        ("buy", D(97), D(3)),
        ("sell", D(104), D(3)),
    ]
    assert all(o.time_in_force == "Alo" for o in q)
    rounded = s.quotes(book("100.005"), client.current)
    assert rounded[0].price <= D("100.005") * D("0.99")
    assert rounded[1].price >= D("100.005") * D("1.02")


def test_signed_perpetual_inventory_skew_saturates_and_targets_flat():
    client, s = setup(inventory_skew_enabled=True)
    long = s.quotes(book(), replace(client.current, positions={"BTC": D(1)}))
    short = s.quotes(book(), replace(client.current, positions={"BTC": D(-1)}))
    assert [o.quantity for o in long] == [D("0.5"), D("1.5")]
    assert [o.quantity for o in short] == [D("1.5"), D("0.5")]
    assert [o.side for o in s.quotes(book(), replace(client.current, positions={"BTC": D(2)}))] == [
        "sell"
    ]


def test_side_bands_minimum_spread_and_small_order_refusal():
    c, high = setup(price_ceiling=D(100))
    assert [q.side for q in high.quotes(book(), c.current)] == ["sell"]
    c, low = setup(price_floor=D(100), minimum_spread=D("0.03"))
    assert [(q.side, q.price) for q in low.quotes(book(), c.current)] == [("buy", D(97))]
    tiny = PureMarketMakingStrategy(replace(high.context, min_notional=D(1000)), high.config)
    assert tiny.quotes(book(), c.current) == ()
    assert high.quotes(replace(book(), asks=()), c.current) == ()


@pytest.mark.parametrize(
    "options",
    [
        {"order_amount": D("NaN")},
        {"bid_spread": D(1)},
        {"order_levels": True},
        {"order_levels": 51},
        {"order_refresh_time": float("inf")},
        {"inventory_range_multiplier": D(0)},
        {"hanging_orders_enabled": True},
        {"inventory_target_position": D("Infinity")},
        {"price_floor": D(2), "price_ceiling": D(1)},
    ],
)
def test_bad_configuration_refuses(options):
    with pytest.raises(ValueError):
        PureMarketMakingConfig(**options)


@pytest.mark.asyncio
async def test_refresh_cancels_acknowledged_orders_before_new_quotes():
    c, s = setup(order_refresh_time=10)
    await s.on_book(book())
    await s.on_tick(0)
    await s.on_tick(9)
    assert [name for name, _ in c.calls] == ["place"]
    await s.on_tick(10)
    assert [name for name, _ in c.calls] == ["place", "cancel", "place"]
    first = {q.client_order_id for q in c.calls[0][1]}
    second = {q.client_order_id for q in c.calls[2][1]}
    assert not first & second


@pytest.mark.asyncio
async def test_fill_and_minimum_spread_trigger_refresh_before_deadline():
    c, s = setup(minimum_spread=D("0.005"))
    await s.on_book(book())
    await s.on_tick(0)
    await s.on_fill(Fill("BTC", "buy", D("0.1"), D(99), "1-0", trade_id="fill-one"))
    c.cancel_outcome = "already_closed"
    await s.on_tick(1)
    assert [name for name, _ in c.calls] == ["place", "cancel", "place"]
    await s.on_book(book("98", 2))
    await s.on_tick(2)
    assert [name for name, _ in c.calls][-2:] == ["cancel", "place"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "place"])
async def test_unknown_execution_freezes_without_replacement_or_retry(failure):
    c, s = setup(order_refresh_time=1)
    await s.on_book(book())
    if failure == "place":
        c.outcome = "timeout"
    else:
        await s.on_tick(0)
        c.cancel_outcome = "unknown"
    with pytest.raises((RuntimeError, TimeoutError)):
        await s.on_tick(1)
    assert s.blocked
    count = len(c.calls)
    await s.on_tick(100)
    assert len(c.calls) == count
    await s.stop()
    assert c.calls[-1] == ("all", "BTC")


@pytest.mark.asyncio
async def test_refresh_replans_after_cancel_await_and_stop_prevents_late_placement():
    c, s = setup(order_refresh_time=1)
    await s.on_book(book())
    await s.on_tick(0)
    c.cancel_gate = asyncio.Event()
    task = asyncio.create_task(s.on_tick(1))
    await asyncio.sleep(0)
    await s.on_book(book("200", 2))
    c.cancel_gate.set()
    await task
    assert c.calls[-1][1][0].price == D(198)

    async def stop_during_account():
        s.blocked = True

    c.on_account = stop_during_account
    before = sum(name == "place" for name, _ in c.calls)
    await s.on_tick(2)
    assert sum(name == "place" for name, _ in c.calls) == before


@pytest.mark.asyncio
async def test_invalid_and_regressed_books_do_not_restore_old_quotes():
    c, s = setup()
    await s.on_book(book(timestamp=2))
    await s.on_tick(0)
    await s.on_book(replace(book(timestamp=3), asks=()))
    await s.on_book(book(timestamp=1))
    await s.on_tick(1)
    assert [name for name, _ in c.calls] == ["place", "cancel"]


def test_yaml_strategy_factory_uses_exact_decimal_and_rejects_unknown_keys():
    from glyde_mm.strategies import create_strategy

    client, original = setup()
    s = create_strategy(
        "pure_market_making",
        original.context,
        {
            "order_amount": "0.123456789123456789",
            "order_refresh_time": "10",
        },
    )
    assert s.config.order_amount == D("0.123456789123456789")
    assert s.config.order_refresh_time == 10
    for values in (
        {"typo": 1},
        {"hanging_orders_enabled": True},
        {"order_levels": "2"},
    ):
        with pytest.raises(ValueError):
            create_strategy("pure_market_making", original.context, values)


@pytest.mark.asyncio
async def test_cancelled_submission_never_creates_a_fresh_attempt():
    client, strategy = setup()
    await strategy.on_book(book())
    client.place_gate = asyncio.Event()
    task = asyncio.create_task(strategy.on_tick(0))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert strategy.blocked
    count = len(client.calls)
    await strategy.on_tick(100)
    assert len(client.calls) == count


async def test_refresh_tolerance_retains_ids_but_age_still_retires_them():
    c, s = setup(order_refresh_time=10, refresh_tolerance_pct=D("1"), max_order_age=25)
    await s.on_book(book())
    await s.on_tick(0)
    original = dict(s._active)
    await s.on_book(book("100.5", 2))
    await s.on_tick(10)
    await s.on_tick(20)
    assert s._active == original
    assert [kind for kind, _ in c.calls] == ["place"]
    await s.on_tick(25)
    assert [kind for kind, _ in c.calls] == ["place", "cancel", "place"]
    assert not set(original) & s._active.keys()


async def test_tolerance_uses_exact_percentage_boundary():
    c, s = setup(order_refresh_time=1, refresh_tolerance_pct=D("1"))
    await s.on_book(book())
    await s.on_tick(0)
    await s.on_book(book("101", 2))
    await s.on_tick(1)
    assert [kind for kind, _ in c.calls] == ["place"]
    await s.on_book(book("101.02", 3))
    await s.on_tick(2)
    assert [kind for kind, _ in c.calls] == ["place", "cancel", "place"]


async def test_inventory_size_change_bypasses_price_tolerance():
    c, s = setup(order_refresh_time=1, refresh_tolerance_pct=D("100"), inventory_skew_enabled=True)
    await s.on_book(book())
    await s.on_tick(0)
    c.current = replace(c.current, positions={"BTC": D(1)})
    await s.on_tick(1)
    assert [kind for kind, _ in c.calls] == ["place", "cancel", "place"]
    assert c.calls[-1][1][0].quantity == D("0.5")


async def test_fill_bypasses_tolerance_before_refresh_interval():
    c, s = setup(order_refresh_time=100, refresh_tolerance_pct=D("100"))
    await s.on_book(book())
    await s.on_tick(0)
    await s.on_fill(Fill("BTC", "buy", D(1), D(99), "1-0"))
    await s.on_tick(1)
    assert [kind for kind, _ in c.calls] == ["place", "cancel", "place"]


async def test_age_crossing_during_tolerance_account_wait_cannot_retain():
    c, s = setup(order_refresh_time=1, refresh_tolerance_pct=D("100"), max_order_age=10)
    clock = [0.0]
    s.context = replace(s.context, clock=lambda: clock[0])
    await s.on_book(book())
    await s.on_tick(0)

    async def advance_clock():
        clock[0] = 11

    c.on_account = advance_clock
    await s.on_tick(1)
    assert [kind for kind, _ in c.calls] == ["place", "cancel", "place"]


async def test_max_age_forces_cleanup_even_when_timer_is_not_due_and_cancel_is_unknown():
    c, s = setup(order_refresh_time=1000, refresh_tolerance_pct=D("100"), max_order_age=10)
    await s.on_book(book())
    await s.on_tick(0)
    c.cancel_outcome = "unknown"
    with pytest.raises(RuntimeError, match="cancellation not confirmed"):
        await s.on_tick(10)
    await s.on_tick(1000)
    assert s.blocked
    assert [kind for kind, _ in c.calls] == ["place", "cancel"]


@pytest.mark.parametrize(
    "name", ["pure_market_making", "avellaneda_market_making", "inventory_skewed", "grid"]
)
def test_all_strategy_configs_share_refresh_controls(name):
    from glyde_mm.strategies.factory import create_strategy

    values = {"refresh_tolerance_pct": "0.1", "max_order_age": 45}
    if name == "grid":
        values.update(lower_price="80", upper_price="120", order_amount="1")
    c, s = setup()
    selected = create_strategy(name, s.context, values)
    assert selected.config.refresh_tolerance_pct == D("0.1")
    assert selected.config.max_order_age == 45
    for field, value in (
        ("refresh_tolerance_pct", "-1"),
        ("max_order_age", True),
        ("max_order_age", 0),
    ):
        with pytest.raises(ValueError):
            create_strategy(name, s.context, {**values, field: value})


async def test_ping_pong_partials_alternate_and_duplicate_or_foreign_fills_cannot_turn_it():
    c, s = setup(ping_pong_enabled=True, order_refresh_time=1000, refresh_tolerance_pct=D(100))
    await s.on_book(book())
    await s.on_tick(0)
    buy = Fill("BTC", "buy", D(".1"), D(99), "1-0", trade_id="buy-one")
    sell = Fill("BTC", "sell", D(".1"), D(102), "1-1", trade_id="sell-one")
    await s.on_fill(buy)
    assert {order.side for order in s.quotes(book(), c.current)} == {"sell"}
    await s.on_fill(sell)
    await s.on_fill(buy)  # A duplicate must not override the subsequent opposite-side print.
    await s.on_fill(replace(buy, symbol="ETH", trade_id="foreign"))
    assert {order.side for order in s.quotes(book(), c.current)} == {"buy"}
    await s.on_tick(1)
    assert [kind for kind, _ in c.calls] == ["place", "cancel", "place"]
    assert {order.side for order in c.calls[-1][1]} == {"buy"}
    await s.on_fill(replace(sell, trade_id="late-cancelled-order"))
    assert {order.side for order in s.quotes(book(), c.current)} == {"buy"}
    await s.on_fill(Fill("BTC", "buy", D(".1"), D(99), "2-0", trade_id="buy-two"))
    await s.on_tick(2)
    assert {order.side for order in c.calls[-1][1]} == {"sell"}
    await s.on_tick(3)
    assert len(c.calls) == 5  # No opposite fill: no arbitrary return to two-sided quoting.


async def test_ping_pong_unknown_cancel_blocks_opposite_replacement():
    c, s = setup(ping_pong_enabled=True)
    await s.on_book(book())
    await s.on_tick(0)
    await s.on_fill(Fill("BTC", "buy", D(".1"), D(99), "1-0", trade_id="fill"))
    c.cancel_outcome = "unknown"
    with pytest.raises(RuntimeError, match="cancellation not confirmed"):
        await s.on_tick(1)
    await s.on_tick(100)
    assert s.blocked and [kind for kind, _ in c.calls] == ["place", "cancel"]


@pytest.mark.parametrize(
    "patch",
    [
        {"trade_id": None},
        {"side": "sell"},
        {"quantity": D(2)},
        {"price": D(100)},
        {"client_order_id": "different"},
    ],
)
async def test_ping_pong_unidentified_or_mismatched_own_fill_freezes(patch):
    c, s = setup(ping_pong_enabled=True)
    await s.on_book(book())
    await s.on_tick(0)
    with pytest.raises(RuntimeError, match="ping_pong_fill_evidence"):
        await s.on_fill(
            replace(Fill("BTC", "buy", D(".1"), D(99), "1-0", trade_id="fill"), **patch)
        )
    await s.on_tick(1)
    assert s.blocked and [kind for kind, _ in c.calls] == ["place"]
    await s.stop()
    assert c.calls[-1] == ("all", "BTC")


async def test_bands_pull_wrong_side_before_timer_and_restore_on_reentry():
    c, s = setup(
        price_floor=D(95),
        price_ceiling=D(105),
        order_refresh_time=1000,
        refresh_tolerance_pct=D(100),
        order_levels=3,
        order_level_amount=D(1),
    )
    await s.on_book(book())
    await s.on_tick(0)
    await s.on_book(book("105", 2))
    await s.on_tick(1)
    assert [kind for kind, _ in c.calls] == ["place", "cancel", "place"]
    assert [order.side for order in c.calls[-1][1]] == ["sell"] * 3
    assert [order.quantity for order in c.calls[-1][1]] == list(map(D, (1, 2, 3)))
    await s.on_book(book("95", 3))
    await s.on_tick(2)
    assert [order.side for order in c.calls[-1][1]] == ["buy"] * 3
    await s.on_book(book("100", 4))
    await s.on_tick(3)
    assert len(c.calls[-1][1]) == 6
    assert [kind for kind, _ in c.calls] == [
        "place",
        "cancel",
        "place",
        "cancel",
        "place",
        "cancel",
        "place",
    ]


async def test_ping_pong_never_overrides_a_price_band():
    c, s = setup(ping_pong_enabled=True, price_ceiling=D(105), order_refresh_time=1000)
    await s.on_book(book("105"))
    await s.on_tick(0)
    assert [order.side for order in c.calls[-1][1]] == ["sell"]
    await s.on_fill(Fill("BTC", "sell", D(".1"), D("107.1"), "1-0", trade_id="sell"))
    await s.on_tick(1)
    assert not s._active  # Opposite bids remain prohibited at the ceiling.
    assert [kind for kind, _ in c.calls] == ["place", "cancel"]
    await s.on_book(book("100", 2))
    await s.on_tick(2)
    assert [order.side for order in c.calls[-1][1]] == ["buy"]


@pytest.mark.parametrize("flag", ["true", 1, None])
def test_ping_pong_flag_refuses_boolean_coercion(flag):
    with pytest.raises(ValueError, match="boolean"):
        PureMarketMakingConfig(ping_pong_enabled=flag)


async def managed_paper_strategy(**values):
    from glyde_mm.paper import PaperExchange
    from glyde_mm.risk import RiskGuard, RiskLimits, RiskManagedClient

    clock = [1000.0]

    class Simulated(PaperExchange):
        async def account(self):
            return replace(await super().account(), received_at=clock[0])

    raw = Simulated(D(1000), clock_ms=lambda: int(clock[0] * 1000))
    raw.use_trade_tape()
    observed = replace(book(timestamp=1000000), received_at=1000.0)
    await raw.on_book(observed)
    guard = RiskGuard(
        RiskLimits(max_position=D("2.5"), max_order_notional=D(1000), max_daily_loss=D(100)),
        clock=lambda: clock[0],
        wall_clock=lambda: clock[0],
    )
    guard.update_book(observed)
    client = RiskManagedClient(raw, guard, cleanup_symbol="BTC")
    context = StrategyContext(client, "BTC", D(".01"), D(".001"), clock=lambda: clock[0])
    selected = PureMarketMakingConfig(
        order_amount=D(1), bid_spread=D(".01"), ask_spread=D(".02"), **values
    )
    strategy = PureMarketMakingStrategy(context, selected)
    await strategy.on_book(observed)
    return raw, client, strategy, clock


async def test_ping_pong_real_tape_partial_fills_and_risk_boundary_preserve_cash():
    from glyde_mm.models import Trade

    raw, client, s, clock = await managed_paper_strategy(ping_pong_enabled=True)
    try:
        await s.on_tick()
        assert {order.side for order in await raw.open_orders()} == {"buy", "sell"}
        clock[0] = 1000.001
        await raw.on_trade(Trade("BTC", "sell", D(".25"), D(98), 1000001, "first"))
        await s.on_fill(raw.fills[-1])
        clock[0] = 1000.25
        await s.on_tick()
        assert [order.side for order in await raw.open_orders()] == ["sell"]
        assert (await raw.account()).positions["BTC"] == D(".25")
        clock[0] = 1000.251
        await raw.on_trade(Trade("BTC", "buy", D(".25"), D(103), 1000251, "opposite"))
        await s.on_fill(raw.fills[-1])
        clock[0] = 1000.5
        await s.on_tick()
        assert [order.side for order in await raw.open_orders()] == ["buy"]
        assert (await raw.account()).positions["BTC"] == 0
        assert (await raw.account()).equity == D("1000.75")
        assert len(raw.fills) == 2 and len(client.guard._order_attempts) == 4
    finally:
        await s.stop()
        await client.close()
    assert not await raw.open_orders()


async def test_scaled_ladder_cannot_escape_shared_position_cap():
    raw, client, s, _ = await managed_paper_strategy(order_levels=2, order_level_amount=D(1))
    try:
        with pytest.raises(RuntimeError, match="max_position"):
            await s.on_tick()
        assert not raw.fills and not await raw.open_orders()
        assert (await raw.account()).equity == 1000
        assert not client.guard._order_attempts
    finally:
        await s.stop()
        await client.close()
