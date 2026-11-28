"""Glyde caps a signed order/cancel/modify action at 16 entries, and refuses larger ones whole.
Its per-account buckets (place burst 15, cancel burst 10) set the smaller sizes actually sent.

Seen live on 26 Nov 2026: 8 levels per side (16 quotes) ran; 10 or more ended ~30s later with
``RiskViolation: cancel_outcome_unknown`` and no quote placed. The refusal left the bot hunting
order history for cloids that never existed.
"""

import asyncio
import json
import socket
from decimal import Decimal as D

import httpx
import pytest
from test_risk import FakeClient, Order, guard

from glyde_mm.client import (
    MAX_PLACE_ENTRIES,
    ActionRefused,
    EngineUnavailable,
    OneApiClient,
    StreamDisconnected,
)
from glyde_mm.config import Config, MarketConfig, RiskConfig
from glyde_mm.models import ModifyRequest, OrderRequest, bot_client_order_id
from glyde_mm.risk import RiskManagedClient, RiskViolation
from glyde_mm.testing import FakeOneApiServer

OWNER = "0x" + "1" * 40
REFUSED = {"status": "err", "response": "Order rejected by current engine constraints"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("batch tests cannot open network sockets")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "0x" + "3" * 64)


def live_config():
    return Config(
        mode="live",
        owner=OWNER,
        vault_address="0x" + "4" * 40,
        markets=(MarketConfig("BTC", D(10), {"order_amount": "0.01"}),),
        risk=RiskConfig(max_position=D(10), max_order_notional=D(25), max_total_notional=D(1000)),
    )


def quotes(count):
    return [
        OrderRequest(
            "BTC",
            "buy" if i % 2 else "sell",
            D("0.01"),
            D(90 - i) if i % 2 else D(110 + i),
            client_order_id=bot_client_order_id(),
            time_in_force="Alo",
        )
        for i in range(count)
    ]


async def exercise(orders, *, refuse=(), modify=False):
    """Send through the real client to the offline venue; refuse the listed action indexes."""
    fake = FakeOneApiServer(user=OWNER)
    fake.set_book("BTC", bids=[("99", "10")], asks=[("101", "10")])
    transport, actions = fake.http_transport(), []

    async def handle(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/exchange"):
            actions.append(body["action"])
            if len(actions) - 1 in refuse:
                return httpx.Response(200, json=REFUSED)
        return await transport.handle_async_request(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = OneApiClient(live_config(), http_client=http)
        if not modify:
            return await client.place_orders(orders), actions, fake
        placed = await client.place_orders(orders)
        actions.clear()
        results = await client.modify_orders(
            [ModifyRequest(row.order_id, order) for row, order in zip(placed, orders, strict=True)]
        )
        return results, actions, fake


@pytest.mark.parametrize("count", [1, 16, 17, 20, 40])
async def test_quotes_are_split_into_actions_glyde_accepts(count):
    results, actions, fake = await exercise(quotes(count))
    sizes = [len(action["orders"]) for action in actions]
    assert all(1 <= size <= MAX_PLACE_ENTRIES for size in sizes)
    assert sum(sizes) == count and len(sizes) == -(-count // MAX_PLACE_ENTRIES)
    assert [row.status for row in results] == ["resting"] * count
    assert len(fake.info({"type": "openOrders", "user": OWNER})) == count


async def test_modifies_are_split_too():
    results, actions, _ = await exercise(quotes(20), modify=True)
    assert [len(action["modifies"]) for action in actions] == [15, 5]
    assert [row.status for row in results] == ["resting"] * 20


async def test_offline_venue_refuses_an_oversized_action_like_glyde():
    fake = FakeOneApiServer(user=OWNER)
    fake.set_book("BTC", bids=[("99", "10")], asks=[("101", "10")])
    order = {"a": 0, "b": True, "p": "90", "s": "0.01", "r": False, "t": {"limit": {"tif": "Alo"}}}
    assert fake.exchange({"action": {"type": "order", "orders": [order] * 17}}) == REFUSED
    assert fake.info({"type": "openOrders", "user": OWNER}) == []


async def test_a_refused_first_action_raises_with_glydes_reason():
    with pytest.raises(ActionRefused, match="Order rejected by current engine constraints"):
        await exercise(quotes(20), refuse={0})


async def test_a_refused_later_action_keeps_placed_orders_and_sends_no_more():
    results, actions, fake = await exercise(quotes(40), refuse={1})
    assert len(actions) == 2  # The third action is never sent.
    assert [row.status for row in results] == ["resting"] * 15 + ["rejected"] * 25
    assert len(fake.info({"type": "openOrders", "user": OWNER})) == 15


class RefusingClient(FakeClient):
    def __init__(self, error):
        super().__init__()
        self.error = error

    async def place_orders(self, orders):
        self.placed.extend(orders)
        raise self.error


def test_a_refused_placement_is_not_an_unknown_outcome():
    async def scenario():
        g, _ = guard()
        raw = RefusingClient(ActionRefused("exchange refused the action: Glyde: no"))
        client = RiskManagedClient(raw, g)
        with pytest.raises(RiskViolation, match="order_refused") as stopped:
            await client.place_orders([Order(), Order()])
        assert isinstance(stopped.value.__cause__, ActionRefused)
        # Nothing was placed: no reservation, no durable intent, no cancel for its cloids.
        assert g.pending == {} and g.execution_cancellations(None) == ()
        assert raw.cancelled == [] and g.halted == "order_refused"

    asyncio.run(scenario())


def test_an_engine_halt_refusal_retires_quotes_like_a_stream_drop():
    async def scenario():
        g, _ = guard()
        raw = RefusingClient(EngineUnavailable("exchange refused the action: engine unavailable"))
        client = RiskManagedClient(raw, g)
        with pytest.raises(StreamDisconnected):
            await client.place_orders([Order()])
        assert g.pending == {} and g.halted is None and g.resume_after_disconnect()

    asyncio.run(scenario())
