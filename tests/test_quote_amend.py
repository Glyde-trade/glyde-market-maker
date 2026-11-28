"""Changed-level refreshes amend moved quotes in place (28 Nov 2026).

Each refresh cancelled the old level and then placed the new one. Prime serializes a
placement behind an in-flight cancel, so a refreshed level took ~1s longer to reach
glydeBook. An amend keeps the order and its IDs and sends one request instead of two.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from test_pure_market_making import book
from test_quote_refresh_budget import actions, setup
from test_risk import FakeClient, Order, guard

from glyde_mm.models import ModifyRequest, OrderRequest, OrderResult
from glyde_mm.paper import PaperExchange
from glyde_mm.risk import RiskManagedClient, RiskViolation


async def placed(**options):
    client, now, strategy = setup(**options)
    await strategy.on_book(book("100"))
    await strategy.on_tick(0)
    return client, now, strategy


async def test_a_moved_level_is_amended_with_its_ids_and_new_price():
    client, now, strategy = await placed()
    before = dict(strategy._active)
    now[0] = 31.0
    await strategy.on_book(book("102", timestamp=2))
    await strategy.on_tick(31)
    assert actions(client) == [("place", 4), ("modify", 4)]
    sent = client.calls[1][1]
    assert {row.order_id for row in sent} == set(before)
    for row in sent:
        original = before[row.order_id]
        assert row.order.client_order_id == original.client_order_id
        assert (row.order.side, row.order.quantity) == (original.side, original.quantity)
        assert row.order.price != original.price and row.order.time_in_force == "Alo"
    assert set(strategy._active) == set(before)
    assert {o.price for o in strategy._active.values()} == {row.order.price for row in sent}
    assert all(strategy._placed_at[oid] == 31.0 for oid in strategy._active)


async def test_a_refused_amend_cancels_its_original_before_placing_the_target():
    client, now, strategy = await placed()
    client.modify_outcome = "rejected"
    now[0] = 31.0
    await strategy.on_book(book("102", timestamp=2))
    await strategy.on_tick(31)
    assert actions(client) == [("place", 4), ("modify", 4), ("cancel", 4), ("place", 4)]
    cancelled = {row.order_id for row in client.calls[2][1]}
    assert cancelled == {row.order_id for row in client.calls[1][1]}
    assert not cancelled & set(strategy._active) and len(strategy._active) == 4
    assert not strategy.blocked


async def test_a_filled_amend_drops_the_quote_and_asks_for_a_fresh_refresh():
    client, now, strategy = await placed()
    client.modify_outcome = "filled"
    now[0] = 31.0
    await strategy.on_book(book("102", timestamp=2))
    await strategy.on_tick(31)
    assert actions(client) == [("place", 4), ("modify", 4)]
    assert not strategy._active and strategy._needs_refresh


async def test_a_lost_amend_reply_blocks_the_strategy_instead_of_resending():
    client, now, strategy = await placed()
    client.modify_outcome = "timeout"
    now[0] = 31.0
    await strategy.on_book(book("102", timestamp=2))
    with pytest.raises(TimeoutError):
        await strategy.on_tick(31)
    assert strategy.blocked
    assert actions(client) == [("place", 4), ("modify", 4)]


async def test_an_aged_quote_is_replaced_not_amended():
    client, now, strategy = await placed(max_order_age=30.0)
    now[0] = 31.0
    await strategy.on_book(book("102", timestamp=2))
    await strategy.on_tick(31)
    assert actions(client) == [("place", 4), ("cancel", 4), ("place", 4)]


def amend(order_id: str, order) -> ModifyRequest:
    return ModifyRequest(order_id, order)


class AmendingClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.modified = []
        self.modify_result = "resting"

    async def modify_orders(self, orders):
        self.modified.extend(orders)
        if self.modify_result == "timeout":
            raise TimeoutError("fake exchange accepted but response was lost")
        return [
            SimpleNamespace(
                status=self.modify_result,
                order_id=row.order_id,
                client_order_id=row.order.client_order_id,
            )
            for row in orders
        ]


def test_risk_boundary_swaps_the_reservation_to_the_amended_terms():
    async def scenario():
        g, _ = guard()
        raw = AmendingClient()
        client = RiskManagedClient(raw, g)
        await client.place_orders([Order(quantity=D("0.06"), client_order_id="one")])
        resting = Order(quantity=D("0.06"), client_order_id="one")
        raw.orders = [resting]
        # At the cap with the original: the amended order replaces it, never adds to it.
        moved = Order(quantity=D("0.06"), price=D("98"), client_order_id="one")
        results = await client.modify_orders([amend("0", moved)])
        assert [r.status for r in results] == ["resting"]
        assert g.pending["one"].price == D("98") and g.halted is None
        assert raw.modified == [amend("0", moved)]

    asyncio.run(scenario())


def test_risk_boundary_refuses_only_an_amend_over_the_position_cap():
    async def scenario():
        g, _ = guard()
        raw = AmendingClient()
        client = RiskManagedClient(raw, g)
        await client.place_orders([Order(quantity=D("0.04"), client_order_id="one")])
        await client.place_orders([Order(quantity=D("0.04"), client_order_id="two")])
        raw.orders = [
            Order(quantity=D("0.04"), client_order_id="one"),
            Order(quantity=D("0.04"), client_order_id="two"),
        ]
        # 0.08 in place of 0.04 beside the other 0.04 buy exceeds the 0.1 cap: that amend
        # alone is refused, as an over-cap placement is, and the original stays reserved.
        (result,) = await client.modify_orders(
            [amend("0", Order(quantity=D("0.08"), client_order_id="one"))]
        )
        assert (result.status, result.error) == ("rejected", "max_position")
        assert g.pending["one"].quantity == D("0.04") and g.halted is None
        assert raw.modified == []

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["timeout", "unknown"])
def test_an_unknown_amend_outcome_latches_and_cancels_the_original(status):
    async def scenario():
        g, _ = guard()
        raw = AmendingClient()
        raw.cancel_result = "unknown"
        client = RiskManagedClient(raw, g)
        await client.place_orders([Order(client_order_id="one")])
        raw.orders = [Order(client_order_id="one")]
        raw.modify_result = status
        with pytest.raises((RiskViolation, TimeoutError)):
            await client.modify_orders([amend("0", Order(price=D("98"), client_order_id="one"))])
        assert g.halted == "order_outcome_unknown"
        assert raw.all_cancels == 1
        assert len(raw.modified) == 1
        with pytest.raises(RiskViolation):
            await client.modify_orders([amend("0", Order(price=D("97"), client_order_id="one"))])
        assert len(raw.modified) == 1

    asyncio.run(scenario())


async def test_paper_amend_moves_the_order_in_place_and_a_crossing_amend_removes_it():
    client = PaperExchange()
    await client.on_book(book("100"))
    (rest,) = await client.place_orders(
        [OrderRequest("BTC", "buy", D("1"), D("99"), client_order_id="0xabc")]
    )
    assert rest.status == "resting"
    (moved,) = await client.modify_orders(
        [ModifyRequest(rest.order_id, OrderRequest("BTC", "buy", D("1"), D("98.5")))]
    )
    assert moved == OrderResult("resting", rest.order_id, "0xabc")
    (row,) = await client.open_orders()
    assert (row.order_id, row.price, row.client_order_id) == (rest.order_id, D("98.5"), "0xabc")
    (crossed,) = await client.modify_orders(
        [ModifyRequest(rest.order_id, OrderRequest("BTC", "buy", D("1"), D("101")))]
    )
    assert crossed.status == "rejected"
    assert await client.open_orders() == []
    (gone,) = await client.modify_orders(
        [ModifyRequest(rest.order_id, OrderRequest("BTC", "buy", D("1"), D("98")))]
    )
    assert gone.status == "rejected"
