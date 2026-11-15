"""Offline wire and matching contracts; any attempted network connect fails."""

import asyncio
import json
import socket
from decimal import Decimal

import httpx
import pytest

from glyde_mm.testing import FakeOneApiServer

ALICE = "0x" + "1" * 40
BOB = "0x" + "2" * 40
CLOID = "0x" + "a" * 32


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("offline tests must not connect to a network")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)


@pytest.fixture
def server():
    fake = FakeOneApiServer()
    fake.set_book("BTC", bids=[("99", "2")], asks=[("101", "2")])
    return fake


def wire(*, price="100", size="1", buy=True, tif="Gtc", **extra):
    return {
        "a": 0,
        "b": buy,
        "p": price,
        "s": size,
        "r": False,
        "t": {"limit": {"tif": tif}},
        **extra,
    }


def place(server, order, user=ALICE):
    result = server.exchange({"action": {"type": "order", "orders": [order]}}, user=user)
    return result["response"]["data"]["statuses"][0]


async def receive(ws):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=1))


async def subscribe(ws, subscription):
    await ws.send(json.dumps({"method": "subscribe", "subscription": subscription}))
    ack = await receive(ws)
    assert ack == {
        "channel": "subscriptionResponse",
        "data": {"method": "subscribe", "subscription": subscription},
    }


def test_reads_exact_book_meta_mids_and_flat_account(server):
    assert server.info({"type": "meta"})["universe"][0]["szDecimals"] == 5
    assert server.info({"type": "allMids"}) == {"BTC": "100"}
    book = server.info({"type": "l2Book", "coin": "BTC"})
    assert book["levels"] == [[{"px": "99", "sz": "2", "n": 1}], [{"px": "101", "sz": "2", "n": 1}]]
    assert server.info({"type": "openOrders", "user": ALICE}) == []
    account = server.info({"type": "clearinghouseState", "user": ALICE})
    assert account["marginSummary"]["accountValue"] == "10000"
    assert account["assetPositions"] == []


async def test_http_transport_supports_v1_without_listening_or_fallback(server):
    async with httpx.AsyncClient(
        transport=server.http_transport(), base_url="https://offline.invalid"
    ) as client:
        response = await client.post("/v1/info", json={"type": "allMids"})
        assert response.json() == {"BTC": "100"}
        result = await client.post(
            "/v1/exchange",
            json={
                "action": {"type": "order", "orders": [wire()]},
                "nonce": 1,
                "signature": {"r": "fixture", "s": "fixture", "v": 27},
            },
        )
        assert result.json()["response"]["data"]["statuses"][0].get("resting")
        assert (await client.get("/v1/info")).status_code == 404
        assert (await client.post("/unexpected", json={})).status_code == 404
        assert (await client.post("/v1/info", content="not json")).status_code == 422
        assert (await client.post("/v1/info", json=[])).status_code == 422
    assert len(server.requests) == 2


def test_post_only_refuses_without_changing_book_or_position(server):
    before = server.info({"type": "l2Book", "coin": "BTC"})
    assert "error" in place(server, wire(price="101", tif="Alo"))
    assert server.info({"type": "l2Book", "coin": "BTC"}) == before
    assert server.info({"type": "userFills", "user": ALICE}) == []


def test_gtc_partial_then_price_time_priority_and_exact_remainders(server):
    first = place(server, wire(size="0.3"))["resting"]["oid"]
    second = place(server, wire(size="0.7"), user=BOB)["resting"]["oid"]
    server.market_order("BTC", buy=False, size="0.4", user="counterparty")
    alice = server.info({"type": "userFills", "user": ALICE})
    bob = server.info({"type": "openOrders", "user": BOB})
    assert alice[0]["oid"] == first and alice[0]["sz"] == "0.3"
    assert bob[0]["oid"] == second and Decimal(bob[0]["sz"]) == Decimal("0.6")
    assert (
        server.info({"type": "clearinghouseState", "user": ALICE})["assetPositions"][0]["position"][
            "szi"
        ]
        == "0.3"
    )


def test_ioc_partial_never_rests_and_gtc_remainder_does(server):
    result = place(server, wire(price="101", size="3", tif="Ioc"))
    assert result["filled"]["totalSz"] == "2"
    assert result["filled"]["avgPx"] == "101"
    assert server.info({"type": "openOrders", "user": ALICE}) == []
    server.set_book("BTC", bids=[("99", "2")], asks=[("101", "2")])
    result = place(server, wire(price="101", size="3"))
    order = server.info({"type": "openOrders", "user": ALICE})[0]
    assert order["oid"] == result["resting"]["oid"] and order["sz"] == "1"


def test_better_prices_execute_first(server):
    server.set_book("BTC", bids=[("98", "2")], asks=[("102", "2"), ("101", "0.5")])
    result = place(server, wire(price="102", size="1", tif="Ioc"))
    assert result["filled"]["avgPx"] == "101.5"


def test_reduce_only_never_opens_or_flips_and_realized_pnl_is_exact(server):
    assert "error" in place(server, wire(buy=False, price="99", r=True))
    place(server, wire(price="101", size="0.3", tif="Ioc"))
    result = place(server, wire(price="99", buy=False, size="1", tif="Ioc", r=True))
    assert result["filled"]["totalSz"] == "0.3"
    account = server.info({"type": "clearinghouseState", "user": ALICE})
    assert account["assetPositions"] == []
    assert Decimal(account["marginSummary"]["accountValue"]) == Decimal("9999.4")
    fills = server.info({"type": "userFills", "user": ALICE})
    assert Decimal(fills[-1]["closedPnl"]) == Decimal("-0.6")


def test_cancel_by_id_cloid_owner_isolation_and_modify(server):
    oid = place(server, wire(c=CLOID))["resting"]["oid"]
    denied = server.exchange(
        {"action": {"type": "cancel", "cancels": [{"a": 0, "o": oid}]}}, user=BOB
    )
    assert "error" in denied["response"]["data"]["statuses"][0]
    result = server.exchange(
        {
            "action": {
                "type": "batchModify",
                "modifies": [{"oid": oid, "order": wire(price="99.5", c=CLOID)}],
            }
        }
    )
    replacement = result["response"]["data"]["statuses"][0]["resting"]["oid"]
    assert replacement != oid
    assert server.info({"type": "openOrders", "user": ALICE})[0]["cloid"] == CLOID
    result = server.exchange(
        {"action": {"type": "cancelByCloid", "cancels": [{"asset": 0, "cloid": CLOID}]}}
    )
    assert result["response"]["data"]["statuses"] == ["success"]
    assert server.info({"type": "openOrders", "user": ALICE}) == []


@pytest.mark.parametrize(
    "bad",
    [wire(size="0.000001"), wire(price="NaN"), wire(size="-1"), wire(buy="yes"), wire(c="nope")],
)
def test_invalid_order_is_explicit_refusal(server, bad):
    assert "error" in place(server, bad)
    assert server.info({"type": "openOrders", "user": ALICE}) == []


@pytest.mark.parametrize("bad", [wire(price="NaN"), wire(price="102", tif="Alo")])
def test_invalid_modify_preserves_existing_order(server, bad):
    oid = place(server, wire())["resting"]["oid"]
    result = server.exchange(
        {"action": {"type": "batchModify", "modifies": [{"oid": oid, "order": bad}]}}
    )
    assert "error" in result["response"]["data"]["statuses"][0]
    assert server.info({"type": "openOrders", "user": ALICE})[0]["oid"] == oid


async def test_ws_ack_baseline_fill_deltas_and_private_isolation(server):
    async with server.connect_ws() as ws, server.connect_ws() as bob:
        await subscribe(ws, {"type": "userFills", "user": ALICE})
        assert (await receive(ws))["data"] == {"user": ALICE, "isSnapshot": True, "fills": []}
        await subscribe(ws, {"type": "orderUpdates", "user": ALICE})
        await subscribe(bob, {"type": "userFills", "user": BOB})
        await receive(bob)
        oid = place(server, wire(size="0.25", c=CLOID))["resting"]["oid"]
        assert (await receive(ws))["data"][0]["status"] == "open"
        server.market_order("BTC", buy=False, size="0.1", user=BOB)
        fill = await receive(ws)
        assert fill["channel"] == "userFills" and fill["data"]["isSnapshot"] is False
        assert fill["data"]["fills"][0]["oid"] == oid
        assert fill["data"]["fills"][0]["cloid"] == CLOID
        assert (await receive(ws))["data"][0]["order"]["sz"] == "0.15"
        assert (await receive(bob))["data"]["fills"][0]["side"] == "A"
        await subscribe(ws, {"type": "l2Book", "coin": "BTC"})
        assert (await receive(ws))["data"]["levels"][0][0]["px"] == "100"


async def test_private_open_orders_pushed_even_before_account_read(server):
    async with server.connect_ws() as ws:
        await subscribe(ws, {"type": "openOrders", "user": ALICE})
        assert (await receive(ws))["data"]["orders"] == []
        place(server, wire())
        assert (await receive(ws))["data"]["orders"][0]["limitPx"] == "100"


async def test_disconnect_wakes_pending_read_and_reconnect_has_immutable_baseline(server):
    async with server.connect_ws() as ws:
        await subscribe(ws, {"type": "l2Book", "coin": "BTC"})
        snapshot = await receive(ws)
        pending = asyncio.create_task(ws.recv())
        await asyncio.sleep(0)
        await server.disconnect()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(pending, 1)
    server.advance_time(1500)
    server.set_book("BTC", bids=[("100", "2")], asks=[("102", "2")])
    async with server.connect_ws() as ws:
        await subscribe(ws, {"type": "l2Book", "coin": "BTC"})
        current = await receive(ws)
        assert current["data"]["time"] == snapshot["data"]["time"] + 1500
        assert snapshot["data"]["levels"][0][0]["px"] == "99"


async def test_unsubscribe_and_ping(server):
    async with server.connect_ws() as ws:
        sub = {"type": "l2Book", "coin": "BTC"}
        await subscribe(ws, sub)
        await receive(ws)
        await ws.send(json.dumps({"method": "unsubscribe", "subscription": sub}))
        assert (await receive(ws))["data"]["method"] == "unsubscribe"
        server.set_book("BTC", bids=[], asks=[])
        await ws.send('{"method":"ping"}')
        assert await receive(ws) == {"channel": "pong"}


def test_crossing_external_update_fills_user_and_invalid_seed_is_atomic(server):
    oid = place(server, wire())["resting"]["oid"]
    with pytest.raises(ValueError, match="crossed"):
        server.set_book("BTC", bids=[("105", "1")], asks=[("100", "1")])
    assert server.info({"type": "openOrders", "user": ALICE})[0]["oid"] == oid
    server.set_book("BTC", bids=[("98", "1")], asks=[("100", "0.2")])
    assert server.info({"type": "userFills", "user": ALICE})[0]["sz"] == "0.2"


def test_independent_servers_have_no_shared_state(server):
    other = FakeOneApiServer()
    place(server, wire())
    assert other.info({"type": "openOrders", "user": ALICE}) == []


@pytest.mark.parametrize("oid", [None, True, False, "", "not-an-id", 0])
def test_invalid_cancel_cannot_match_missing_cloid(server, oid):
    resting = place(server, wire())["resting"]["oid"]
    result = server.exchange({"action": {"type": "cancel", "cancels": [{"a": 0, "o": oid}]}})
    assert "error" in result["response"]["data"]["statuses"][0]
    assert server.info({"type": "openOrders", "user": ALICE})[0]["oid"] == resting


def test_default_market_control_is_a_counterparty_not_a_self_trade(server):
    oid = place(server, wire(size="1"))["resting"]["oid"]
    server.market_order("BTC", buy=False, size="0.2")
    fills = server.info({"type": "userFills", "user": ALICE})
    assert len(fills) == 1 and fills[0]["oid"] == oid and fills[0]["sz"] == "0.2"
