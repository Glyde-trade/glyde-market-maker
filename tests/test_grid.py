from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal as D

import pytest

from glyde_mm.models import Account, Book, Level, OrderResult
from glyde_mm.strategies.grid import GridConfig, GridStrategy
from glyde_mm.strategies.pure_market_making import StrategyContext


class FakeClient:
    def __init__(self):
        self.calls = []
        self.refuse_cancel = False

    async def account(self):
        return Account(D("1000"))

    async def place_orders(self, orders):
        self.calls.append(("place", list(orders)))
        return [OrderResult("resting", str(i), o.client_order_id) for i, o in enumerate(orders)]

    async def cancel_orders(self, orders):
        from glyde_mm.models import CancelResult

        self.calls.append(("cancel", list(orders)))
        return [
            CancelResult("rejected" if self.refuse_cancel else "cancelled", o.order_id)
            for o in orders
        ]

    async def cancel_all(self, symbol=None):
        self.calls.append(("cancel_all", symbol))


def setup(**kwargs):
    client = FakeClient()
    context = StrategyContext(client, "BTC", D("1"), D("0.01"), clock=lambda: 10.0)
    config = GridConfig(D("90"), D("110"), D("0.1"), **kwargs)
    strategy = GridStrategy(context, config)
    book = Book("BTC", (Level(D("99"), D("2")),), (Level(D("101"), D("2")),), 1000)
    return strategy, client, book


def test_fixed_levels_never_quote_at_mid_and_preserve_exact_size():
    strategy, _, book = setup()
    orders = strategy.quotes(book, Account(D("1000")))
    assert [(o.side, o.price) for o in orders] == [
        ("buy", D("90")),
        ("buy", D("95")),
        ("sell", D("105")),
        ("sell", D("110")),
    ]
    assert all(o.quantity == D("0.1") for o in orders)


def test_outside_range_or_wrong_market_has_no_quotes():
    strategy, _, book = setup()
    assert not strategy.quotes(replace(book, symbol="ETH"), Account(D("1000")))
    assert not strategy.quotes(
        replace(book, bids=(Level(D("111"), D("2")),), asks=(Level(D("113"), D("2")),)),
        Account(D("1000")),
    )


def test_coarse_tick_deduplicates_levels_and_minimum_spread_filters_near_quotes():
    strategy, _, book = setup(minimum_spread=D("0.06"))
    assert [o.price for o in strategy.quotes(book, Account(D("1000")))] == [D("90"), D("110")]
    strategy.context = replace(strategy.context, price_tick=D("10"))
    quotes = strategy.quotes(book, Account(D("1000")))
    assert len({(o.side, o.price) for o in quotes}) == len(quotes)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"levels": 1},
        {"levels": True},
        {"levels": 51},
        {"order_refresh_time": float("inf")},
        {"minimum_spread": D("NaN")},
    ],
)
def test_invalid_grid_configuration_refuses(kwargs):
    with pytest.raises(ValueError):
        setup(**kwargs)


def test_lifecycle_cancels_before_replacement_and_blocks_after_unknown_cancel():
    async def scenario():
        strategy, client, book = setup()
        await strategy.on_book(book)
        await strategy.on_tick(10)
        assert client.calls[0][0] == "place"
        client.refuse_cancel = True
        with pytest.raises(RuntimeError, match="cancellation"):
            await strategy.on_tick(41)
        await strategy.on_tick(80)
        assert [name for name, _ in client.calls] == ["place", "cancel"]
        await strategy.stop()
        assert client.calls[-1] == ("cancel_all", "BTC")

    asyncio.run(scenario())


def test_quantization_never_escapes_an_unaligned_configured_range():
    strategy, _, book = setup()
    strategy.grid = replace(strategy.grid, lower_price=D("91"), upper_price=D("109"))
    strategy.context = replace(strategy.context, price_tick=D("10"))
    quotes = strategy.quotes(book, Account(D("1000")))
    assert all(D("91") <= order.price <= D("109") for order in quotes)
