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
