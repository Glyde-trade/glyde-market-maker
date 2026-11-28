"""A subscription refused with retryAfter reconnects like a socket drop; other refusals stop."""

import asyncio
import json
import socket
import time
from dataclasses import replace
from decimal import Decimal as D

import pytest
from test_multi_market_live import MarketSocket
from test_quote_resilience import CANCEL_BOUND_SECONDS, FreshWire, episode, until
from test_quote_resilience import config as live_config
from test_stream_reconnect import Venue, events, session
from test_stream_reconnect import config as paper_config
from test_stream_reconnect import until as run_until

from glyde_mm import runner
from glyde_mm.client import (
    OneApiError,
    StreamDisconnected,
    SubscriptionDeferred,
    reconnectable,
    subscription_refusal,
)
from glyde_mm.runner import risk_state_path

# The frame production sent for userFills on 2026-11-22, with a test-sized retry hint.
DEFERRED = {
    "channel": "error",
    "data": "Glyde: projection unavailable",
    "reasonCode": "PROJECTION_UNAVAILABLE",
    "retryAfter": 0.3,
}
REFUSED = {"channel": "error", "data": "Glyde: subscription refused"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("deferral tests cannot open network sockets")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "0x" + "3" * 64)
    monkeypatch.setenv("GLYDE_READ_API_KEY", "read-fixture")
    monkeypatch.setattr(runner, "RECONNECT_INITIAL_SECONDS", 0.01)


@pytest.mark.parametrize(
    ("frame", "deferred", "delay"),
    [
        (DEFERRED, True, 0.3),
        ({**DEFERRED, "retryAfter": 1}, True, 1.0),
        ({**DEFERRED, "retryAfter": 10**9}, True, 60.0),
        ({"channel": "error", "reasonCode": "PROJECTION_UNAVAILABLE"}, True, 0.0),
        ({"channel": "error", "reasonCode": "SOMETHING_ELSE", "retryAfter": 2}, True, 2.0),
        ({**DEFERRED, "retryAfter": True, "reasonCode": "X"}, False, None),
        ({**DEFERRED, "retryAfter": -1, "reasonCode": "X"}, False, None),
        ({**DEFERRED, "retryAfter": "1", "reasonCode": "X"}, False, None),
        (REFUSED, False, None),
        # Engine-halt codes defer even without a retry hint (26 Nov 2026).
        ({"channel": "error", "reasonCode": "ENGINE_HALTED"}, True, 0.0),
        ({"channel": "error", "reasonCode": "ADAPTER_INTEGRITY_CONFLICT"}, True, 0.0),
        # Glyde's uncoded projection refusals resubscribe from a fresh snapshot.
        ({"channel": "error", "data": "Glyde: resnapshot required"}, True, 0.0),
        ({"channel": "error", "data": "Glyde: projection unavailable"}, True, 0.0),
        ({"channel": "error", "data": "Glyde: subscription capacity exceeded"}, False, None),
    ],
)
def test_only_retry_hints_or_transient_codes_defer(frame, deferred, delay):
    error = subscription_refusal(frame)
    assert isinstance(error, SubscriptionDeferred) is deferred
    assert reconnectable(error) is deferred
    if deferred:
        assert error.retry_after == delay


def test_untrusted_reason_code_is_not_echoed():
    error = subscription_refusal({**DEFERRED, "reasonCode": "secret key=0xabc"})
    assert isinstance(error, SubscriptionDeferred)
    assert error.reason_code == "UNSPECIFIED" and "0xabc" not in str(error)


class DeferringVenue(Venue):
    """Records connect times; can refuse the first subscription before any snapshot."""

    def __init__(self, *, refuse_first=False):
        super().__init__()
        self.refuse_first = refuse_first
        self.connected_at = []

    def connect_ws(self, url="", **kwargs):
        ws = super().connect_ws(url, **kwargs)
        self.connected_at.append(time.monotonic())
        if self.refuse_first and len(self.sockets) == 1:
            ws.push(DEFERRED)
        return ws


async def test_paper_deferred_subscription_retires_quotes_and_resubscribes(tmp_path):
    selected = paper_config()
    async with session(tmp_path, selected) as (venue, paper, task, stop, lines):
        await run_until(task, lambda: len(paper.open_before_place) == 1)
        venue.sockets[0].push(DEFERRED)
        await run_until(task, lambda: len(paper.open_before_place) == 2)
        assert len(venue.sockets) == 2 and not task.done()
        assert venue.sockets[1].subscriptions == venue.sockets[0].subscriptions
        assert paper.open_before_place == [0, 0]  # Retired before the fresh book quoted.
        assert any("PROJECTION_UNAVAILABLE" in line and "reconnecting" in line for line in lines)
        assert not any("projection unavailable" in line for line in lines)  # No raw frame text.
        stop.set()
        await asyncio.wait_for(task, 2)
    rows = events(tmp_path)
    names = [row["event"] for row in rows]
    assert names.count("disconnected") == names.count("reconnected") == 1
    assert rows[-1]["outcome"] == "stopped"


async def test_paper_retry_after_is_the_minimum_reconnect_delay(tmp_path, monkeypatch):
    venue = DeferringVenue()
    monkeypatch.setattr("test_stream_reconnect.Venue", lambda: venue)
    async with session(tmp_path, paper_config()) as (_, paper, task, stop, _):
        await run_until(task, lambda: len(paper.open_before_place) == 1)
        refused_at = time.monotonic()
        venue.sockets[0].push(DEFERRED)
        await run_until(task, lambda: len(paper.open_before_place) == 2)
        # Backoff alone would be ~10 ms; the server's retryAfter sets the floor.
        assert venue.connected_at[1] - refused_at >= DEFERRED["retryAfter"]
        stop.set()
        await asyncio.wait_for(task, 2)


async def test_paper_first_subscription_deferral_retries_before_quoting(tmp_path, monkeypatch):
    venue = DeferringVenue(refuse_first=True)
    monkeypatch.setattr("test_stream_reconnect.Venue", lambda: venue)
    async with session(tmp_path, paper_config()) as (_, paper, task, stop, lines):
        await run_until(task, lambda: len(paper.open_before_place) == 1)
        assert len(venue.sockets) == 2 and not task.done()
        assert venue.connected_at[1] - venue.connected_at[0] >= DEFERRED["retryAfter"]
        assert any("PROJECTION_UNAVAILABLE" in line for line in lines)
        stop.set()
        await asyncio.wait_for(task, 2)
    assert events(tmp_path)[-1]["outcome"] == "stopped"


async def test_paper_refusal_without_retry_hint_still_ends_the_run(tmp_path):
    async with session(tmp_path, paper_config()) as (venue, paper, task, _, _):
        await run_until(task, lambda: len(paper.open_before_place) == 1)
        venue.sockets[0].push(REFUSED)
        with pytest.raises(OneApiError, match="subscription refused"):
            await asyncio.wait_for(task, 2)
        assert len(venue.sockets) == 1
    assert await paper.open_orders() == []
    assert events(tmp_path)[-1]["outcome"] == "transport_error"


class DeferredFillsSocket(MarketSocket):
    """The production refusal: every subscription is admitted except userFills."""

    async def send(self, raw):
        frame = json.loads(raw)
        if frame.get("subscription", {}).get("type") == "userFills":
            self.queue.put_nowait(DEFERRED)
            return
        await super().send(raw)


class FirstFillsDeferredWire(FreshWire):
    def connect(self, url, **kwargs):
        socket = super().connect(url, **kwargs)
        return DeferredFillsSocket(self) if self.connections == 1 else socket


async def test_live_deferred_subscription_reconciles_then_requotes(tmp_path):
    wire, selected = FreshWire(), live_config()
    async with episode(wire, selected, tmp_path) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        originals = set(wire.orders())
        wire.socket.queue.put_nowait(DEFERRED)
        await until(wire, lambda: not wire.orders())
        await until(wire, lambda: len(wire.orders()) == 4)
        assert set(wire.orders()).isdisjoint(originals)
        assert wire.connections == 2 and not task.done()
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()
    wire.assert_no_duplicate_cancels()


async def test_live_engine_halt_safety_frame_pauses_then_requotes(tmp_path, monkeypatch):
    monkeypatch.setattr("glyde_mm.live_state.ENGINE_HALT_RETRY_SECONDS", 0.2)
    wire, selected = FreshWire(), live_config()
    async with episode(wire, selected, tmp_path) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        wire.socket.queue.put_nowait({"channel": "glydeSafety", "data": {"ready": False}})
        await until(wire, lambda: not wire.orders())
        await until(wire, lambda: len(wire.orders()) == 4)
        assert wire.connections == 2 and not task.done()
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()


async def test_live_first_connect_userfills_deferral_retries_and_quotes(tmp_path):
    wire, selected = FirstFillsDeferredWire(), live_config()
    async with episode(wire, selected, tmp_path) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        assert wire.connections == 2 and not task.done()
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()


class LateDeferredFillsSocket(MarketSocket):
    """Production order: books arrive first, then the userFills refusal during startup."""

    async def send(self, raw):
        frame = json.loads(raw)
        if frame.get("subscription", {}).get("type") == "userFills":
            asyncio.get_running_loop().call_later(0.05, self.queue.put_nowait, DEFERRED)
            return
        await super().send(raw)


class FirstFillsLateDeferredWire(FreshWire):
    def connect(self, url, **kwargs):
        socket = super().connect(url, **kwargs)
        return LateDeferredFillsSocket(self) if self.connections == 1 else socket


async def test_live_first_connect_deferral_after_books_retries_and_quotes(tmp_path):
    wire, selected = FirstFillsLateDeferredWire(), live_config()
    async with episode(wire, selected, tmp_path) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        assert wire.connections == 2 and not task.done()
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()


async def test_live_refusal_without_retry_hint_still_ends_the_run(tmp_path):
    wire, selected = FreshWire(), live_config()
    async with episode(wire, selected, tmp_path) as (task, _):
        await until(wire, lambda: len(wire.orders()) == 4)
        wire.socket.queue.put_nowait(REFUSED)
        await until(wire, lambda: not wire.orders())
        with pytest.raises(OneApiError):
            await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert wire.connections == 1


class BooksOnlySocket(MarketSocket):
    """A projection outage: the market group publishes, the owner snapshot never arrives."""

    async def send(self, raw):
        frame = json.loads(raw)
        if frame["method"] == "ping":
            self.queue.put_nowait({"channel": "pong"})
            return
        self.subscriptions.append(frame["subscription"])
        if len(self.subscriptions) == 6:
            self.fixture.emit_book("BTC")
            self.fixture.emit_book("ETH")


class ProjectionOutageWire(FreshWire):
    """Production 22 Nov 2026: refused sockets, then books without an owner cut, then recovery."""

    def __init__(self, *, refused=(), books_only=()):
        super().__init__()
        self.refused, self.books_only = refused, books_only

    def connect(self, url, **kwargs):
        socket = super().connect(url, **kwargs)
        if self.connections in self.refused:
            raise OSError("synthetic projection outage")
        return BooksOnlySocket(self) if self.connections in self.books_only else socket


async def test_live_projection_outage_keeps_reconnecting_until_the_owner_cut_returns(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("glyde_mm.live_state.ENGINE_HALT_RETRY_SECONDS", 0.05)
    wire, selected = ProjectionOutageWire(refused=range(2, 9), books_only={9}), live_config()
    async with episode(wire, selected, tmp_path) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        originals = set(wire.orders())
        wire.socket.queue.put_nowait({"channel": "glydeSafety", "data": {"ready": False}})
        await until(wire, lambda: not wire.orders())
        await run_until(task, lambda: len(wire.orders()) == 4, seconds=8)
        assert set(wire.orders()).isdisjoint(originals)
        assert wire.connections == 10 and not task.done()
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()
    assert json.loads(risk_state_path(selected, tmp_path).read_text())["execution"] is None


async def test_live_outage_past_the_stale_pause_window_ends_without_a_latch(tmp_path, monkeypatch):
    monkeypatch.setattr("glyde_mm.live_state.ENGINE_HALT_RETRY_SECONDS", 0.05)
    wire, selected, lines = ProjectionOutageWire(refused=range(2, 10**6)), live_config(), []
    selected = replace(selected, risk=replace(selected.risk, max_stale_pause_seconds=D(1)))
    async with episode(wire, selected, tmp_path, report=lines.append) as (task, _):
        await until(wire, lambda: len(wire.orders()) == 4)
        wire.socket.queue.put_nowait({"channel": "glydeSafety", "data": {"ready": False}})
        dropped = time.monotonic()
        with pytest.raises(StreamDisconnected):
            await asyncio.wait_for(task, 4)
        assert time.monotonic() - dropped >= 1 and wire.connections > 2
    assert not wire.orders()
    assert any("reconnect window" in line for line in lines)
    assert json.loads(risk_state_path(selected, tmp_path).read_text())["execution"] is None


async def test_live_first_connect_timeout_retries_and_quotes(tmp_path):
    # 28 Nov 13:17: the first connect's owner cut never arrived, the account read timed out
    # and the bot exited for systemd to restart. It now retries in-process.
    wire, selected = ProjectionOutageWire(books_only={1}), live_config()
    async with episode(wire, selected, tmp_path) as (task, stop):
        await run_until(task, lambda: len(wire.orders()) == 4, seconds=8)
        assert wire.connections == 2 and not task.done()
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()
