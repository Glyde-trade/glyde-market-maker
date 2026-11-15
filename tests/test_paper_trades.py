"""Synthetic recorded public-tape controls; no network or historical-profit claim."""

import json
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest

from glyde_mm.client import OneApiError, trades_from_wire
from glyde_mm.models import Book, CancelRequest, Level, OrderRequest, Trade
from glyde_mm.paper import PaperExchange

FIXTURE = Path(__file__).parent / "fixtures/paper-trades.jsonl"


def book(timestamp=1000, bid=99, ask=101):
    return Book("BTC", (Level(D(bid), D(1)),), (Level(D(ask), D(1)),), timestamp)


def trade(tid="1", *, timestamp=1001, side="sell", price="99", size="1"):
    return Trade("BTC", side, D(size), D(price), timestamp, tid)


async def paper():
    result = PaperExchange(clock_ms=lambda: 1000)
    result.use_trade_tape()
    await result.on_book(book())
    return result


async def test_recorded_tape_queue_partial_fifo_and_realized_pnl():
    simulator = await paper()
    placed = await simulator.place_orders(
        [
            OrderRequest("BTC", "buy", D("0.5"), D(99)),
            OrderRequest("BTC", "buy", D("0.5"), D(99)),
            OrderRequest("BTC", "sell", D("0.5"), D(101)),
        ]
    )
    prints = trades_from_wire([json.loads(line) for line in FIXTURE.read_text().splitlines()])
    await simulator.on_trade(prints[0])
    assert simulator.fills == []  # First 0.6 only consumes the displayed queue ahead.
    await simulator.on_trade(prints[1])
    assert [fill.quantity for fill in simulator.fills] == [D("0.2")]
    await simulator.on_trade(prints[1])  # Same timestamp/identity cannot replay volume.
    for row in prints[2:]:
        await simulator.on_trade(row)
    assert [fill.quantity for fill in simulator.fills] == list(
        map(D, [".2", ".25", ".3", ".3", ".25"])
    )
    assert [fill.price for fill in simulator.fills] == list(
        map(D, ["99", "101", "99", "99", "101"])
    )
    remaining = await simulator.open_orders()
    assert len(remaining) == 1 and remaining[0].order_id == placed[1].order_id
    assert remaining[0].quantity == D("0.2")
    account = await simulator.account()
    assert account.positions == {"BTC": D("0.3")}
    assert account.equity == D("10001.3")
    assert (await simulator.cancel_orders([CancelRequest("BTC", placed[0].order_id)]))[
        0
    ].status == "already_closed"
    await simulator.cancel_all()
    assert not await simulator.open_orders() and not simulator._queue_ahead


async def test_books_do_not_also_fill_tape_orders_and_side_is_aggressor():
    simulator = await paper()
    await simulator.place_orders([OrderRequest("BTC", "buy", D(1), D(99))])
    await simulator.on_book(book(1001, 97, 98))
    assert not simulator.fills
    await simulator.on_trade(trade(side="buy", price="98", size="9", timestamp=1002))
    assert not simulator.fills
    await simulator.on_trade(trade("2", price="98", size=".25", timestamp=1003))
    assert simulator.fills[0].quantity == D(".25")
    assert simulator.fills[0].price == D(99)  # Resting limit, not price improvement at the print.


async def test_source_cut_fence_old_snapshot_duplicates_and_reordering():
    simulator = await paper()
    await simulator.on_trade(trade(timestamp=1001))
    await simulator.place_orders([OrderRequest("BTC", "buy", D(1), D(99))])
    for row in [trade("2", timestamp=1000, price="98"), trade("3", timestamp=1001, price="98")]:
        await simulator.on_trade(row)
    assert not simulator.fills
    await simulator.on_trade(trade("4", timestamp=1002, price="98", size=".25"))
    await simulator.on_trade(trade("4", timestamp=1002, price="98", size=".25"))
    await simulator.on_trade(trade("5", timestamp=1001, price="98"))
    assert sum(row.quantity for row in simulator.fills) == D(".25")
    # A replacement cannot consume the prior print, even if received twice.
    await simulator.cancel_all()
    await simulator.place_orders([OrderRequest("BTC", "buy", D(1), D(99))])
    await simulator.on_trade(trade("6", timestamp=1002, price="98"))
    assert len(simulator.fills) == 1


async def test_trade_budget_shared_across_prices_and_symbols():
    simulator = await paper()
    await simulator.place_orders(
        [
            OrderRequest("BTC", "buy", D(".3"), D(98)),
            OrderRequest("BTC", "buy", D(".3"), D(99)),
        ]
    )
    await simulator.on_trade(replace(trade(price="97"), symbol="ETH"))
    assert not simulator.fills
    await simulator.on_trade(trade(price="97", size=".4"))
    assert [(row.price, row.quantity) for row in simulator.fills] == [
        (D(99), D(".3")),
        (D(98), D(".1")),
    ]
    assert sum(row.quantity for row in simulator.fills) == D(".4")


async def test_reduce_only_cannot_flip_on_tape():
    simulator = await paper()
    await simulator.place_orders([OrderRequest("BTC", "buy", D(".3"), D(101), time_in_force="Ioc")])
    await simulator.place_orders([OrderRequest("BTC", "sell", D(1), D(101), reduce_only=True)])
    await simulator.on_trade(trade(side="buy", price="102", size="2"))
    assert (await simulator.account()).positions["BTC"] == 0
    assert simulator.fills[-1].quantity == D(".3")
    await simulator.on_trade(trade("2", side="buy", price="102", size="2", timestamp=1002))
    assert len(simulator.fills) == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"side": "bad"},
        {"px": "NaN"},
        {"sz": "0"},
        {"time": True},
        {"tid": True},
        {"tid": "-1"},
        {"tid": "x"},
        {"tid": "1" * 65},
    ],
)
def test_invalid_trade_frame_fails_before_any_partial_batch_application(patch):
    row = {"coin": "BTC", "side": "A", "px": "99", "sz": "1", "time": 1001, "tid": 1}
    with pytest.raises(OneApiError, match="invalid public trades"):
        trades_from_wire([row, {**row, **patch}])


async def test_no_midrun_fill_model_switch():
    simulator = PaperExchange()
    await simulator.on_book(book())
    await simulator.place_orders([OrderRequest("BTC", "buy", D(1), D(99))])
    with pytest.raises(ValueError, match="before trading"):
        simulator.use_trade_tape()
