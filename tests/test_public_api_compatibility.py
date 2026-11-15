"""Replay frozen public wire bytes through the real client, without the fake server."""

from __future__ import annotations

import json
import socket
from contextlib import asynccontextmanager
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from glyde_mm.client import OneApiClient, OneApiError, book_from_wire, trades_from_wire
from glyde_mm.config import Config

FIXTURE = Path(__file__).parent / "fixtures/one-api-public/session.jsonl"


def transcript():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


@pytest.fixture(autouse=True)
def public_offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("public compatibility replay cannot open sockets or read credentials")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(Config, "secret", forbidden)


class RecordedSocket:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []
        self.closed = False

    async def send(self, value):
        self.sent.append(json.loads(value))

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.frames)
        except StopIteration:
            raise StopAsyncIteration from None


@asynccontextmanager
async def replay_client(rows, *, websocket_frames=None):
    http_rows = [r for r in rows if r["transport"] == "http"]
    calls, connections = [], []
    requests = [r["frame"] for r in rows if r.get("direction") == "send"]
    frames = [json.dumps(r["frame"]) for r in rows if r.get("direction") == "receive"]
    recorded_socket = RecordedSocket(frames if websocket_frames is None else websocket_frames)

    def handler(request):
        assert request.method == "POST" and request.url.path == "/v1/info"
        assert not request.headers.get("authorization")
        assert not request.headers.get("x-glyde-api-key")
        body = json.loads(request.content)
        matching = [r for r in http_rows if r["request"] == body]
        assert len(matching) == 1, "unexpected/private HTTP request"
        calls.append(body)
        return httpx.Response(200, json=matching[0]["response"])

    @asynccontextmanager
    async def connect(url, **kwargs):
        assert url == "wss://api.glyde.trade/v1/ws"
        assert kwargs["additional_headers"] == {}
        assert kwargs["max_queue"] == 32 and kwargs["max_size"] == 512 * 1024
        connections.append(url)
        try:
            yield recorded_socket
        finally:
            recorded_socket.closed = True

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with OneApiClient(Config(), http_client=http, connect_ws=connect) as client:
            yield client, calls, connections, recorded_socket, requests


async def test_recorded_public_http_meta_book_and_mids_exact_decimals():
    async with replay_client(transcript()) as (client, calls, connections, _, _):
        meta = await client.meta()
        assert [(r["name"], r["szDecimals"]) for r in meta["universe"]] == [("BTC", 5), ("ETH", 4)]
        assert await client._asset("ETH") == (1, 4)
        book = await client.book("BTC")
        assert book.symbol == "BTC" and book.timestamp_ms == 1700000000000
        assert [level.price for level in book.bids] == [D("65432.12345"), D("65431")]
        assert book.bids[0].quantity == D("2.00001")
        assert book.asks[0].price == D("65433.12345")
        assert book.mid == D("65432.62345")
        assert await client.all_mids() == {"BTC": D("65432.62345"), "ETH": D("3200.2002")}
        assert calls == [{"type": "meta"}, {"type": "l2Book", "coin": "BTC"}, {"type": "allMids"}]
        assert not connections


async def test_recorded_public_websocket_acks_snapshot_update_and_trade_order():
    async with replay_client(transcript()) as (client, calls, connections, ws, requests):
        subscriptions = [r["subscription"] for r in requests]
        frames = [frame async for frame in client.stream(subscriptions)]
        assert ws.sent == requests and ws.closed
        assert len(connections) == 1 and not calls
        assert [f["data"] for f in frames if f["channel"] == "subscriptionResponse"] == requests
        assert [f["channel"] for f in frames] == [
            "subscriptionResponse",
            "l2Book",
            "subscriptionResponse",
            "allMids",
            "subscriptionResponse",
            "trades",
            "trades",
            "l2Book",
            "allMids",
        ]
        books = [book_from_wire(f["data"]) for f in frames if f["channel"] == "l2Book"]
        assert [b.timestamp_ms for b in books] == [1700000000000, 1700000000001]
        assert books[0].asks[0].quantity - books[1].asks[0].quantity == D("0.00001")
        trades = [trades_from_wire(f["data"]) for f in frames if f["channel"] == "trades"]
        assert trades[0] == ()
        (trade,) = trades[1]
        assert trade.trade_id == "9007199254740993"  # Beyond a JS exact integer.
        assert (trade.symbol, trade.side, trade.price, trade.quantity) == (
            "BTC",
            "buy",
            D("65433.12345"),
            D("0.00001"),
        )
        assert trade.timestamp_ms == books[1].timestamp_ms
        assert frames[-1]["data"]["mids"] == {"BTC": "65432.62345", "ETH": "3200.2002"}


async def test_recorded_http_and_socket_initial_book_agree():
    async with replay_client(transcript()) as (client, _, _, _, requests):
        http_book = await client.book("BTC")
        frames = [f async for f in client.stream([r["subscription"] for r in requests])]
        ws_book = book_from_wire(next(f["data"] for f in frames if f["channel"] == "l2Book"))
        assert (http_book.symbol, http_book.timestamp_ms, http_book.bids, http_book.asks) == (
            ws_book.symbol,
            ws_book.timestamp_ms,
            ws_book.bids,
            ws_book.asks,
        )


@pytest.mark.parametrize("change", ["missing_levels", "crossed", "nan_quantity"])
async def test_incompatible_book_capture_is_never_a_usable_book(change):
    rows = deepcopy(transcript())
    payload = next(r["response"] for r in rows if r.get("request", {}).get("type") == "l2Book")
    if change == "missing_levels":
        payload["depth"] = payload.pop("levels")
    elif change == "crossed":
        payload["levels"][0][0]["px"] = "70000"
    else:
        payload["levels"][0][0]["sz"] = "NaN"
    async with replay_client(rows) as (client, *_):
        with pytest.raises((OneApiError, KeyError, ValueError)):
            await client.book("BTC")


@pytest.mark.parametrize("frame", [b"{}", "not-json", "[]", '{"channel":"error","data":"refused"}'])
async def test_socket_refusal_binary_or_malformed_frame_closes_without_retry(frame):
    async with replay_client(transcript(), websocket_frames=[frame]) as (
        client,
        calls,
        connections,
        ws,
        _,
    ):
        with pytest.raises(OneApiError):
            async for _ in client.stream([{"type": "trades", "coin": "BTC"}]):
                pytest.fail("invalid frame must not be yielded")
        assert ws.closed and len(connections) == 1 and not calls


@pytest.mark.parametrize(("key", "value"), [("side", "?"), ("tid", 1.5), ("sz", "NaN")])
def test_incompatible_recorded_trade_refuses_entire_batch(key, value):
    trade = next(
        r["frame"]["data"][0]
        for r in transcript()
        if r.get("direction") == "receive"
        and r["frame"].get("channel") == "trades"
        and r["frame"]["data"]
    )
    with pytest.raises(OneApiError):
        trades_from_wire([trade, {**trade, key: value}])
