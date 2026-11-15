"""Offline matching ledger with synthetic sequenced One API publications."""

import asyncio
import json
import time

import httpx
from live_wire import batch, owner_roots

from glyde_mm.testing import FakeOneApiServer


class Socket:
    def __init__(self, fixture):
        self.fixture = fixture
        self.queue = asyncio.Queue()
        self.subscriptions = []

    async def __aenter__(self):
        self.fixture.socket = self
        return self

    async def __aexit__(self, *args):
        pass

    async def send(self, raw):
        frame = json.loads(raw)
        if frame["method"] == "ping":
            self.queue.put_nowait({"channel": "pong"})
            return
        self.subscriptions.append(frame["subscription"])
        if len(self.subscriptions) == 5:
            self.fixture.emit_book()
            self.fixture.emit_owner()

    async def close(self):
        self.queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.queue.get()
        if frame is None:
            raise StopAsyncIteration
        return json.dumps(frame)


class SequencedFake:
    """Wire fixture using the real offline matching ledger, with synthetic batch framing."""

    def __init__(self, *, read_credential="read-fixture"):
        self.read_credential = read_credential
        self.ledger = FakeOneApiServer(
            user="0x" + "1" * 40, timestamp_ms=time.time_ns() // 1_000_000
        )
        self.ledger.set_book("BTC", bids=[("99", "10")], asks=[("101", "10")])
        self.socket = None
        self.sequence = self.book_sequence = 0
        self.requests = []
        self.emit_on_action = True
        self.refreshed = asyncio.Event()

    def connect(self, url, **kwargs):
        assert kwargs["additional_headers"] == {"X-Glyde-API-Key": self.read_credential}
        return Socket(self)

    def emit_book(self):
        self.book_sequence += 1
        frames = batch(
            "mm-book-0",
            {"l2Book": self.ledger.info({"type": "l2Book", "coin": "BTC"})},
            sequence=self.book_sequence,
            previous=self.book_sequence - 1 if self.book_sequence > 1 else None,
        )
        for frame in frames:
            self.socket.queue.put_nowait(frame)

    def emit_owner(self, *, skip=0):
        old = self.sequence
        self.sequence += 1 + skip
        state = self.ledger.info({"type": "clearinghouseState", "user": self.ledger.user})
        state["time"] = self.ledger.now_ms
        roots = owner_roots(
            self.ledger.user,
            orders=self.ledger.info({"type": "openOrders", "user": self.ledger.user}),
            fills=self.ledger.info({"type": "userFills", "user": self.ledger.user}),
        )
        roots["clearinghouseState"]["clearinghouseState"] = state
        for frame in batch(
            "mm-owner",
            roots,
            owner=self.ledger.user,
            sequence=self.sequence,
            previous=old if old else None,
        ):
            self.socket.queue.put_nowait(frame)

    def http(self, request):
        data = json.loads(request.content)
        self.requests.append(data)
        if request.url.path.endswith("/info"):
            result = self.ledger.info(data)
        else:
            result = self.ledger.exchange(data)
            if len([r for r in self.requests if r.get("action", {}).get("type") == "order"]) == 2:
                self.refreshed.set()
            if self.emit_on_action:
                self.emit_owner()
        return httpx.Response(200, json=result)
