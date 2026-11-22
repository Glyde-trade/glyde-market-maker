"""A subscription refused with retryAfter reconnects like a socket drop; other refusals stop."""

import asyncio
import json
import socket
import time

import pytest
from test_multi_market_live import MarketSocket
from test_quote_resilience import CANCEL_BOUND_SECONDS, FreshWire, episode, until
from test_quote_resilience import config as live_config
from test_stream_reconnect import Venue, events, session
from test_stream_reconnect import config as paper_config
from test_stream_reconnect import until as run_until

from glyde_mm import runner
from glyde_mm.client import OneApiError, SubscriptionDeferred, reconnectable, subscription_refusal

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


async def test_live_first_connect_userfills_deferral_retries_and_quotes(tmp_path):
    wire, selected = FirstFillsDeferredWire(), live_config()
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
