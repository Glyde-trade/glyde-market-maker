from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest

from glyde_mm.models import Account, Book, CancelResult, Level, OrderResult
from glyde_mm.strategies import (
    AvellanedaConfig,
    AvellanedaMarketMakingStrategy,
    StrategyContext,
    create_strategy,
)


class Client:
    def __init__(self):
        self.calls = []

    async def account(self):
        return Account(D(1000))

    async def place_orders(self, orders):
        self.calls.append("place")
        return [OrderResult("resting", str(i), o.client_order_id) for i, o in enumerate(orders)]

    async def cancel_orders(self, orders):
        self.calls.append("cancel")
        return [CancelResult("cancelled", o.order_id) for o in orders]

    async def cancel_all(self, symbol=None):
        self.calls.append("all")


def book(mid, timestamp):
    price = D(mid)
    return Book(
        "BTC", (Level(price - 1, D(10)),), (Level(price + 1, D(10)),), timestamp, timestamp / 1000
    )


def setup(**options):
    client = Client()
    context = StrategyContext(client, "BTC", D("0.01"), D("0.001"))
    config = AvellanedaConfig(
        order_amount=D(1),
        volatility_samples=2,
        risk_factor=D(2),
        liquidity_kappa=D(4),
        time_left_fraction=D("0.5"),
        **options,
    )
    return client, AvellanedaMarketMakingStrategy(context, config)


async def warmup(strategy):
    for mid, timestamp in ((100, 0), (101, 1000), (100, 2000)):
        await strategy.on_book(book(mid, timestamp))


@pytest.mark.asyncio
async def test_normalized_hummingbot_formula_golden_and_inventory_direction():
    _, strategy = setup(minimum_spread=D(0))
    await warmup(strategy)
    assert strategy.volatility == 1
    neutral = Account(D(1000))
    reservation, spread = strategy.model_prices(book(100, 2000), neutral)
    assert reservation == 100
    assert abs(spread - D("1.405465108108164381978013115464349")) < D("1e-30")
    assert [(o.price, o.quantity) for o in strategy.quotes(book(100, 2000), neutral)] == [
        (D("99.29"), D(1)),
        (D("100.71"), D(1)),
    ]
    long = replace(neutral, positions={"BTC": D(1)})
    short = replace(neutral, positions={"BTC": D(-1)})
    assert strategy.model_prices(book(100, 2000), long)[0] == D("99.9")
    assert strategy.model_prices(book(100, 2000), short)[0] == D("100.1")
    scaled = Account(D(2000), {"BTC": D(2)})
    assert strategy.model_prices(book(100, 2000), scaled) == strategy.model_prices(
        book(100, 2000), long
    )


@pytest.mark.asyncio
async def test_warmup_zero_volatility_and_source_gap_never_fabricate_quotes():
    client, strategy = setup()
    await strategy.on_book(book(100, 0))
    assert strategy.quotes(book(100, 0), Account(D(1000))) == ()
    await strategy.on_book(book(100, 1000))
    await strategy.on_book(book(100, 2000))
    assert strategy.volatility is None
    assert not strategy.quotes(book(100, 2000), Account(D(1000)))
    await strategy.on_book(book(101, 3000))
    assert strategy.volatility is not None
    before = tuple(strategy._prices)
    await strategy.on_book(book(50, 1000))
    assert tuple(strategy._prices) == before
    await strategy.on_book(book(102, 6000))
    assert strategy.volatility is None
    assert not strategy.quotes(book(102, 6000), Account(D(1000)))
    assert not client.calls


@pytest.mark.asyncio
async def test_sampling_uses_source_intervals_not_callback_frequency():
    _, strategy = setup()
    await strategy.on_book(book(100, 0))
    for now in range(1, 1000):
        await strategy.on_book(book(101, now))
    assert len(strategy._prices) == 1
    assert strategy.volatility is None
    await strategy.on_book(book(101, 1000))
    await strategy.on_book(book(100, 2000))
    assert strategy.volatility == 1


@pytest.mark.asyncio
async def test_model_readiness_retires_live_quotes_and_restarts_only_after_new_warmup():
    client, strategy = setup()
    await strategy.on_book(book(100, 0))
    await strategy.on_tick(0)
    assert not client.calls
    await warmup(strategy)
    await strategy.on_tick(2)
    assert client.calls == ["place"]
    await strategy.on_book(book(100, 5000))
    await strategy.on_tick(5)
    assert client.calls == ["place", "cancel"]
    await strategy.on_book(book(101, 6000))
    await strategy.on_book(book(100, 7000))
    await strategy.on_tick(7)
    assert client.calls == ["place", "cancel", "place"]


@pytest.mark.asyncio
async def test_minimum_side_spread_levels_and_price_bands_use_shared_rules():
    _, strategy = setup(
        minimum_spread=D("0.02"),
        order_levels=2,
        order_level_amount=D(1),
        order_level_spread=D("0.01"),
        price_ceiling=D(100),
    )
    await warmup(strategy)
    assert [
        (o.side, o.price, o.quantity) for o in strategy.quotes(book(100, 2000), Account(D(1000)))
    ] == [
        ("sell", D(102), D(1)),
        ("sell", D(103), D(2)),
    ]


@pytest.mark.parametrize(
    "values",
    [
        {"risk_factor": D(0)},
        {"liquidity_kappa": D(-1)},
        {"risk_factor": D("NaN")},
        {"time_left_fraction": D("1.1")},
        {"volatility_samples": True},
        {"volatility_samples": 4097},
        {"sample_interval_ms": 0},
        {"order_refresh_time": float("inf")},
    ],
)
def test_invalid_model_parameters_refuse(values):
    with pytest.raises(ValueError):
        AvellanedaConfig(**values)


def test_factory_uses_same_context_and_decimal_parameters():
    _, strategy = setup()
    result = create_strategy(
        "avellaneda_market_making",
        strategy.context,
        {"risk_factor": "0.123456789123456789", "volatility_samples": 3},
    )
    assert result.context is strategy.context
    assert result.model.risk_factor == D("0.123456789123456789")
    with pytest.raises(ValueError):
        create_strategy("avellaneda_market_making", strategy.context, {"guessed_kappa": 3})
