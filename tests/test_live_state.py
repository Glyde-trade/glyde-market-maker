from __future__ import annotations

import asyncio
import json
import socket
import time
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D

import httpx
import pytest
from live_wire import batch, owner_roots

from glyde_mm.client import OneApiClient, OneApiError
from glyde_mm.config import Config, RiskConfig
from glyde_mm.live_state import LiveStateClient
from glyde_mm.models import CancelRequest, OrderRequest
from glyde_mm.testing import FakeOneApiServer

OWNER = "0x" + "1" * 40
KEY = "0x" + "3" * 64


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def config():
    return Config(
        mode="live",
        owner=OWNER,
        vault_address="0x" + "4" * 40,
        risk=RiskConfig(max_position=D(1), max_order_notional=D(100)),
    )


def fill(oid=3, tid=1, size="0.1"):
    return {
        "oid": oid,
        "tid": tid,
        "coin": "BTC",
        "side": "B",
        "sz": size,
        "px": "99",
        "fee": "0",
        "cloid": "0x" + "a" * 32,
    }


def seed(state, roots=None):
    for frame in batch("mm-owner", roots or owner_roots(OWNER), owner=OWNER):
        state.consume(frame)


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

    def __init__(self):
        self.ledger = FakeOneApiServer(user=OWNER, timestamp_ms=time.time_ns() // 1_000_000)
        self.ledger.set_book("BTC", bids=[("99", "10")], asks=[("101", "10")])
        self.socket = None
        self.sequence = self.book_sequence = 0
        self.requests = []
        self.emit_on_action = True
        self.refreshed = asyncio.Event()

    def connect(self, url, **kwargs):
        assert kwargs["additional_headers"] == {"X-Glyde-API-Key": "read-fixture"}
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
        state = self.ledger.info({"type": "clearinghouseState", "user": OWNER})
        state["time"] = self.ledger.now_ms
        roots = owner_roots(
            OWNER,
            orders=self.ledger.info({"type": "openOrders", "user": OWNER}),
            fills=self.ledger.info({"type": "userFills", "user": OWNER}),
        )
        roots["clearinghouseState"]["clearinghouseState"] = state
        for frame in batch(
            "mm-owner", roots, owner=OWNER, sequence=self.sequence, previous=old if old else None
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


async def started(monkeypatch):
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", KEY)
    monkeypatch.setenv("GLYDE_READ_API_KEY", "read-fixture")
    fake = SequencedFake()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.http))
    source = OneApiClient(config(), http_client=http, connect_ws=fake.connect)
    await source.meta()
    state = LiveStateClient(source, config())
    stream = state.stream()
    await anext(stream)  # Complete public book.
    assert (await state.account()).equity == D(10000)
    return fake, http, source, state, stream


async def finish(http, state, stream):
    await stream.aclose()
    await state.close()
    await http.aclose()


async def test_real_client_fake_ws_account_reads_never_poll_and_do_not_restamp(monkeypatch):
    fake, http, _, state, stream = await started(monkeypatch)
    try:
        first = await state.account()
        for _ in range(100):
            assert await state.account() == first
            assert await state.open_orders() == []
        assert [row["type"] for row in fake.requests] == ["meta"]
        assert {row["type"] for row in fake.socket.subscriptions} == {
            "l2Book",
            "orderUpdates",
            "userFills",
            "openOrders",
            "clearinghouseState",
        }
    finally:
        await finish(http, state, stream)


async def test_actual_signed_place_fill_account_and_cancel_are_push_only(monkeypatch):
    fake, http, _, state, stream = await started(monkeypatch)
    try:
        cloid = "0x" + "a" * 32
        result = await state.place_orders([OrderRequest("BTC", "buy", D("0.1"), D(100), cloid)])
        oid = result[0].order_id
        assert [o.order_id for o in await state.open_orders()] == [oid]
        fake.ledger.market_order("BTC", buy=False, size="0.1")
        fake.emit_owner()
        await asyncio.sleep(0)
        assert (await state.account()).positions["BTC"] == D("0.1")
        assert await state.open_orders() == []
        requests = len(fake.requests)
        terminal = await state.cancel_orders([CancelRequest("BTC", oid)])
        assert terminal[0].status == "already_closed" and len(fake.requests) == requests
        assert [r.get("type") for r in fake.requests if "type" in r] == ["meta"]
        events = []
        while not state._events.empty():
            events.extend((await anext(stream)).get("fills", []))
        assert len(events) == 1 and events[0].order_id == oid
    finally:
        await finish(http, state, stream)


async def test_shutdown_cancels_original_ids_without_http_discovery(monkeypatch):
    fake, http, _, state, stream = await started(monkeypatch)
    try:
        await state.place_orders([OrderRequest("BTC", "buy", D("0.1"), D(99), "0x" + "a" * 32)])
        await state.open_orders()
        await state.cancel_all("BTC")
        assert await state.open_orders() == []
        assert [row.get("type") for row in fake.requests if "type" in row] == ["meta"]
        assert fake.ledger.info({"type": "openOrders", "user": OWNER}) == []
    finally:
        await finish(http, state, stream)


async def test_gap_stops_reads_writes_but_attempts_known_cancel_without_resnapshot(monkeypatch):
    fake, http, _, state, stream = await started(monkeypatch)
    try:
        await state.place_orders([OrderRequest("BTC", "buy", D("0.1"), D(99), "0x" + "a" * 32)])
        await state.open_orders()
        fake.emit_owner(skip=1)
        await asyncio.sleep(0)
        with pytest.raises(OneApiError):
            await state.account()
        with pytest.raises(OneApiError):
            await state.place_orders([OrderRequest("BTC", "sell", D("0.1"), D(101))])
        with pytest.raises(OneApiError):
            await state.cancel_all("BTC")
        assert fake.ledger.info({"type": "openOrders", "user": OWNER}) == []
        assert len([r for r in fake.requests if r.get("action", {}).get("type") == "order"]) == 1
        assert [r.get("type") for r in fake.requests if "type" in r] == ["meta"]
    finally:
        await finish(http, state, stream)


async def test_disconnect_and_reconnect_delta_gap_cannot_inherit_prior_state(monkeypatch):
    fake, http, source, state, stream = await started(monkeypatch)
    try:
        await fake.socket.close()
        await asyncio.wait_for(state._task, 1)
        with pytest.raises(OneApiError):
            await state.account()
        reconnected = LiveStateClient(source, config())
        frames = batch("mm-owner", owner_roots(OWNER), owner=OWNER, sequence=4, previous=2)
        with pytest.raises(OneApiError):
            for frame in frames:
                reconnected.consume(frame)
        with pytest.raises(OneApiError):
            await reconnected.account()
        assert [r.get("type") for r in fake.requests] == ["meta"]
    finally:
        await finish(http, state, stream)


async def test_atomic_batch_changes_nothing_until_end():
    state = LiveStateClient(None, config())
    seed(state)
    before = await state.account()
    frames = batch(
        "mm-owner", owner_roots(OWNER, equity="900"), owner=OWNER, sequence=2, previous=1
    )
    for frame in frames[:-1]:
        state.consume(frame)
        assert state._account == before
    state.consume(frames[-1])
    assert (await state.account()).equity == D(900)


@pytest.mark.parametrize(
    "mutation",
    [
        "owner",
        "epoch",
        "session",
        "gap",
        "previous",
        "missing",
        "duplicate",
        "cut",
        "safety",
        "unsequenced",
    ],
)
async def test_bad_stream_controls_latch_without_resuming(mutation):
    state = LiveStateClient(None, config())
    seed(state)
    frames = batch("mm-owner", owner_roots(OWNER), owner=OWNER, sequence=2, previous=1)
    if mutation in {"owner", "epoch", "session"}:
        for frame in frames:
            frame["glyde"][mutation] = "foreign"
    elif mutation == "gap":
        for frame in frames:
            frame["glyde"]["sequence"] = "3"
    elif mutation == "previous":
        for frame in frames:
            frame["glyde"]["previous"] = "0"
    elif mutation == "missing":
        frames.pop(1)
    elif mutation == "duplicate":
        frames.insert(1, deepcopy(frames[0]))
    elif mutation == "cut":
        frames[1]["glyde"]["cursor"]["command"] = "99"
    elif mutation == "safety":
        frames = [{"channel": "glydeSafety", "data": {"sourceUnsafe": True}}]
    else:
        frames = [{"channel": "clearinghouseState", "data": {}}]
    with pytest.raises((OneApiError, KeyError)):
        for frame in frames:
            state.consume(frame)
    with pytest.raises(OneApiError):
        seed(state)
    with pytest.raises(OneApiError):
        await state.account()


async def test_source_time_and_local_age_cannot_be_refreshed_by_cache_reads():
    state = LiveStateClient(None, config())
    with pytest.raises(OneApiError, match="stale"):
        seed(state, owner_roots(OWNER, stamp=time.time_ns() // 1_000_000 - 60000))
    state = LiveStateClient(None, config())
    seed(state)
    state._account = replace(state._account, received_at=time.monotonic() - 60)
    with pytest.raises(OneApiError, match="stale"):
        await state.account()


async def test_fill_duplicates_do_not_fire_twice_but_changed_identity_refuses():
    state = LiveStateClient(None, config())
    seed(state)
    for frame in batch(
        "mm-owner", owner_roots(OWNER, fills=[fill()]), owner=OWNER, sequence=2, previous=1
    ):
        state.consume(frame)
    for frame in batch(
        "mm-owner", owner_roots(OWNER, fills=[fill()]), owner=OWNER, sequence=3, previous=2
    ):
        state.consume(frame)
    assert sum(len(row["fills"]) for row in list(state._events._queue)) == 1
    with pytest.raises(OneApiError):
        for frame in batch(
            "mm-owner",
            owner_roots(OWNER, fills=[fill(size="0.2")]),
            owner=OWNER,
            sequence=4,
            previous=3,
        ):
            state.consume(frame)


async def test_actual_live_runner_uses_http_only_for_startup(monkeypatch, tmp_path):
    from glyde_mm.runner import run

    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", KEY)
    monkeypatch.setenv("GLYDE_READ_API_KEY", "read-fixture")
    fake = SequencedFake()
    stop = asyncio.Event()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.http))
    selected = replace(
        config(),
        strategy_config={
            "order_amount": "0.01",
            "bid_spread": "0.02",
            "ask_spread": "0.02",
            "order_refresh_time": 0.01,
        },
    )
    source = OneApiClient(selected, http_client=http, connect_ws=fake.connect)

    async def stop_after_refresh():
        await fake.refreshed.wait()
        stop.set()

    stopper = asyncio.create_task(stop_after_refresh())
    try:
        await asyncio.wait_for(
            run(
                selected,
                state_directory=tmp_path,
                client_factory=lambda _: source,
                stop_event=stop,
                report=lambda _: None,
            ),
            3,
        )
        assert [row["type"] for row in fake.requests if "type" in row] == [
            "clearinghouseState",
            "openOrders",
            "meta",
        ]
        assert (
            len([row for row in fake.requests if row.get("action", {}).get("type") == "order"]) == 2
        )
        assert fake.ledger.info({"type": "openOrders", "user": OWNER}) == []
    finally:
        stopper.cancel()
        await asyncio.gather(stopper, return_exceptions=True)
        await http.aclose()


async def test_incomplete_batch_deadline_blocks_without_more_network_data():
    state = LiveStateClient(None, config())
    seed(state)
    frames = batch("mm-owner", owner_roots(OWNER), owner=OWNER, sequence=2, previous=1)
    state.consume(frames[0])
    metadata, parts, started = state._pending["mm-owner"]
    state._pending["mm-owner"] = metadata, parts, started - 3
    with pytest.raises(OneApiError, match="incomplete"):
        await state.account()
    with pytest.raises(OneApiError):
        state.consume(frames[1])


async def test_bounded_delivery_queue_refuses_instead_of_dropping_fill_updates():
    state = LiveStateClient(None, config())
    seed(state)
    with pytest.raises(OneApiError, match="overflow"):
        for i in range(2, 35):
            for frame in batch(
                "mm-owner", owner_roots(OWNER), owner=OWNER, sequence=i, previous=i - 1
            ):
                state.consume(frame)
    with pytest.raises(OneApiError):
        await state.account()


async def test_cancel_ack_waits_for_push_before_reusing_account_and_orders(monkeypatch):
    fake, http, _, state, stream = await started(monkeypatch)
    try:
        result = await state.place_orders(
            [OrderRequest("BTC", "buy", D("0.1"), D(99), "0x" + "a" * 32)]
        )
        assert await state.open_orders()
        fake.emit_on_action = False
        await state.cancel_orders([CancelRequest("BTC", result[0].order_id)])
        cached = asyncio.create_task(state.account())
        await asyncio.sleep(0)
        assert not cached.done()
        fake.emit_owner()
        assert (await asyncio.wait_for(cached, 1)).positions == {}
        assert await state.open_orders() == []
        assert [row.get("type") for row in fake.requests if "type" in row] == ["meta"]
    finally:
        await finish(http, state, stream)


@pytest.mark.parametrize("invalid", ["absent", 0, True])
async def test_real_server_null_end_index_required_and_bad_end_never_commits(invalid):
    state = LiveStateClient(None, config())
    frames = batch("mm-owner", owner_roots(OWNER), owner=OWNER)
    assert frames[-1]["glyde"]["batch"]["index"] is None
    for frame in frames[:-1]:
        state.consume(frame)
    if invalid == "absent":
        del frames[-1]["glyde"]["batch"]["index"]
    else:
        frames[-1]["glyde"]["batch"]["index"] = invalid
    with pytest.raises(OneApiError):
        state.consume(frames[-1])
    assert state._account is None
