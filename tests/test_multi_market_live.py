"""One actual signed client/WS/cache/risk runner, with independently stale books."""

import asyncio
import json
import socket
import time
from collections import Counter
from decimal import Decimal as D

import httpx
import pytest
from live_fake import SequencedFake, Socket
from live_wire import batch

from glyde_mm.client import OneApiClient
from glyde_mm.config import Config, MarketConfig, RiskConfig
from glyde_mm.runner import run

OWNER = "0x" + "1" * 40


class MarketSocket(Socket):
    async def send(self, raw):
        frame = json.loads(raw)
        if frame["method"] == "ping":
            self.queue.put_nowait({"channel": "pong"})
            return
        self.subscriptions.append(frame["subscription"])
        if len(self.subscriptions) == 6:
            self.fixture.emit_book("BTC")
            self.fixture.emit_book("ETH")
            self.fixture.emit_owner()


class MarketWire(SequencedFake):
    def __init__(self):
        super().__init__()
        self.ledger.set_book("ETH", bids=[("9", "10")], asks=[("11", "10")])
        self.started = asyncio.Event()
        self.connections = 0
        self.books = Counter()
        self.placed = Counter()

    def connect(self, url, **kwargs):
        assert kwargs["additional_headers"] == {"X-Glyde-API-Key": "read-fixture"}
        self.connections += 1
        return MarketSocket(self)

    def emit_book(self, symbol="BTC"):
        self.books[symbol] += 1
        seq = self.books[symbol]
        for frame in batch(
            f"mm-book-{0 if symbol == 'BTC' else 1}",
            {"l2Book": self.ledger.info({"type": "l2Book", "coin": symbol})},
            sequence=seq,
            previous=seq - 1 if seq > 1 else None,
        ):
            self.socket.queue.put_nowait(frame)

    def http(self, request):
        response = super().http(request)
        action = json.loads(request.content).get("action", {})
        if action.get("type") == "order":
            for row in action["orders"]:
                self.placed["BTC" if row["a"] == 0 else "ETH"] += 1
            if self.placed["BTC"] and self.placed["ETH"]:
                self.started.set()
        return response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("multi-market tests must not open sockets")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "0x" + "3" * 64)
    monkeypatch.setenv("GLYDE_READ_API_KEY", "read-fixture")


async def test_one_live_ws_stale_market_retires_other_keeps_quoting_and_recovers(tmp_path):
    wire = MarketWire()
    stop, paused, recovered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    amounts = {"BTC": "0.01", "ETH": "0.1"}
    config = Config(
        mode="live",
        owner=OWNER,
        vault_address="0x" + "4" * 40,
        markets=tuple(
            MarketConfig(s, D(1), {"order_amount": a, "order_refresh_time": 0.01})
            for s, a in amounts.items()
        ),
        risk=RiskConfig(
            max_position=D(1),
            max_order_notional=D(25),
            max_total_notional=D(50),
            stale_book_seconds=D("0.3"),
            max_orders_per_minute=600,
        ),
    )
    btc_when_paused = []

    def report(line):
        if line.startswith("LIVE ETH: quoting paused"):
            btc_when_paused.append(wire.placed["BTC"])
            paused.set()
        if line.startswith("LIVE ETH: quoting resumed"):
            recovered.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire.http)) as http:
        source = OneApiClient(config, http_client=http, connect_ws=wire.connect)

        async def feed():
            async with asyncio.timeout(4):
                await wire.started.wait()
                while not paused.is_set() or wire.placed["BTC"] <= btc_when_paused[0]:
                    wire.ledger.now_ms = time.time_ns() // 1_000_000
                    wire.emit_book("BTC")
                    wire.emit_owner()
                    await asyncio.sleep(0.03)
                # ETH is retired before fresh BTC can continue sharing its account.
                assert not [
                    o
                    for o in wire.ledger.info({"type": "openOrders", "user": OWNER})
                    if o["coin"] == "ETH"
                ]
                eth_before = wire.placed["ETH"]
                while not recovered.is_set() or wire.placed["ETH"] <= eth_before:
                    wire.ledger.now_ms = time.time_ns() // 1_000_000
                    wire.emit_book("BTC")
                    wire.emit_book("ETH")
                    wire.emit_owner()
                    await asyncio.sleep(0.03)
                stop.set()

        feeder = asyncio.create_task(feed())
        try:
            await asyncio.wait_for(
                run(
                    config,
                    state_directory=tmp_path,
                    client_factory=lambda _: source,
                    stop_event=stop,
                    report=report,
                ),
                5,
            )
            await feeder
        finally:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
    assert wire.connections == 1 and recovered.is_set()
    assert len(wire.socket.subscriptions) == 6
    assert wire.ledger.info({"type": "openOrders", "user": OWNER}) == []
    assert Counter(row["type"] for row in wire.requests if "type" in row) == {
        "meta": 1,
        "clearinghouseState": 1,
        "openOrders": 1,
    }
    for envelope in wire.requests:
        if envelope.get("action", {}).get("type") == "order":
            assert envelope["signature"] and envelope["vaultAddress"] == config.vault_address
            for order in envelope["action"]["orders"]:
                assert D(order["s"]) == D(amounts["BTC" if order["a"] == 0 else "ETH"])
