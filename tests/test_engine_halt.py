"""A Glyde engine halt pauses the bot for as long as Glyde reports it; it never ends the run."""

import asyncio
import json
import socket
import time
from dataclasses import replace
from decimal import Decimal as D

import httpx
import pytest
from test_multi_market_live import MarketSocket
from test_quote_resilience import CANCEL_BOUND_SECONDS, OWNER, FreshWire, episode, until
from test_quote_resilience import config as live_config
from test_stream_reconnect import until as run_until

from glyde_mm import runner
from glyde_mm.client import (
    EngineUnavailable,
    OneApiClient,
    OneApiHTTPError,
    StreamDisconnected,
    SubscriptionDeferred,
    engine_unavailable,
)
from glyde_mm.risk import RiskViolation
from glyde_mm.runner import risk_state_path

SAFETY = {"channel": "glydeSafety", "data": {"ready": False, "trading": False}}
# GET /v1/health during the 23 Nov 2026 halt.
HALTED_HEALTH = {"state": "PROJECTION_UNAVAILABLE", "reason": "ADAPTER_INTEGRITY_CONFLICT"}
PROJECTION_REFUSAL = {
    "error": "Glyde: projection unavailable",
    "reasonCode": "PROJECTION_UNAVAILABLE",
    "retryAfter": 1,
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("engine-halt tests cannot open network sockets")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "0x" + "3" * 64)
    monkeypatch.setenv("GLYDE_READ_API_KEY", "read-fixture")
    monkeypatch.setattr(runner, "RECONNECT_INITIAL_SECONDS", 0.05)
    monkeypatch.setattr("glyde_mm.live_state.ENGINE_HALT_RETRY_SECONDS", 0.05)


class HaltedSocket(MarketSocket):
    """A halted engine admits the socket, then publishes only its safety frame."""

    async def send(self, raw):
        frame = json.loads(raw)
        if frame["method"] == "ping":
            self.queue.put_nowait({"channel": "pong"})
            return
        self.subscriptions.append(frame["subscription"])
        if len(self.subscriptions) == 6:
            self.queue.put_nowait(SAFETY)


class HaltingWire(FreshWire):
    """Prime halted: health 503, projection reads refused, signed actions outcome-unknown."""

    def __init__(self):
        super().__init__()
        self.halted = False
        self.refuse_http = True
        self.refused_cancels = 0

    def connect(self, url, **kwargs):
        socket = super().connect(url, **kwargs)
        return HaltedSocket(self) if self.halted else socket

    def http(self, request):
        if request.method == "GET" and request.url.path.endswith("/health"):
            if self.halted:
                return httpx.Response(503, json=HALTED_HEALTH, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"state": "READY", "reason": None})
        if not self.halted or not self.refuse_http:
            return super().http(request)
        self.changed.set()
        if "action" in json.loads(request.content):
            self.refused_cancels += 1
            message = "Glyde: outcome unknown; reconcile the original signed request"
            return httpx.Response(502, json={"status": "err", "response": message})
        return httpx.Response(503, json=PROJECTION_REFUSAL, headers={"Retry-After": "1"})

    def resume_with_fill(self):
        """The engine resumes and a resting bot bid fills before the bot can cancel it."""
        self.halted = False
        self.ledger.now_ms = time.time_ns() // 1_000_000
        self.ledger.market_order("BTC", buy=False, size="0.01")
        self.changed.set()


def halting_config(btc_cap, **risk):
    selected = live_config()
    markets = tuple(
        replace(m, max_position=D(btc_cap)) if m.symbol == "BTC" else m for m in selected.markets
    )
    risk = {"max_stale_pause_seconds": D(1), "max_engine_halt_seconds": D(30), **risk}
    return replace(selected, markets=markets, risk=replace(selected.risk, **risk))


def btc_position(wire):
    state = wire.ledger.info({"type": "clearinghouseState", "user": OWNER})
    return sum(
        (
            D(row["position"]["szi"])
            for row in state["assetPositions"]
            if row["position"]["coin"] == "BTC"
        ),
        D(0),
    )


async def test_halt_longer_than_the_stale_window_with_a_fill_on_resume_keeps_quoting(tmp_path):
    """Production 23 Nov 2026 00:28:45: Prime halted for 5+ min while a cancel was unconfirmed;
    the bot exited with cancel_outcome_unknown and a quote filled at 00:38:54 unmanaged."""
    wire, selected, lines = HaltingWire(), halting_config("0.02"), []
    async with episode(wire, selected, tmp_path, report=lines.append) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        originals = set(wire.orders())
        bid = next(oid for oid, row in wire.orders("BTC").items() if row["side"] == "B")
        wire.halted = True
        wire.socket.queue.put_nowait(SAFETY)
        await run_until(task, lambda: wire.refused_cancels, seconds=2)
        await asyncio.sleep(2.5)  # Halted past max_stale_pause_seconds.
        assert not task.done(), "the bot ended its run during the engine halt"
        assert set(wire.orders()) == originals  # Engine continuity kept every quote resting.

        wire.resume_with_fill()
        assert btc_position(wire) == D("0.01") and bid not in wire.orders()
        await run_until(
            task,
            lambda: len(wire.orders()) == 4 and set(wire.orders()).isdisjoint(originals),
            seconds=8,
        )
        assert not task.done()
        # The fill is inventory: new quotes stay inside the 0.02 cap with the 0.01 long.
        buys = sum(D(r["sz"]) for r in wire.orders("BTC").values() if r["side"] == "B")
        sells = sum(D(r["sz"]) for r in wire.orders("BTC").values() if r["side"] == "A")
        assert btc_position(wire) + buys <= D("0.02")
        assert btc_position(wire) - sells >= D("-0.02")
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()
    assert json.loads(risk_state_path(selected, tmp_path).read_text())["execution"] is None
    assert not any("reconnect window" in line for line in lines)


async def test_fill_on_resume_that_exhausts_the_cap_stops_with_nothing_resting(tmp_path):
    wire, selected = HaltingWire(), halting_config("0.015")
    async with episode(wire, selected, tmp_path) as (task, _):
        await until(wire, lambda: len(wire.orders()) == 4)
        wire.halted = True
        wire.socket.queue.put_nowait(SAFETY)
        await run_until(task, lambda: wire.refused_cancels, seconds=2)
        await asyncio.sleep(1.5)
        assert not task.done()
        wire.resume_with_fill()
        # Another 0.01 bid on a 0.01 long would breach the 0.015 cap: the guard refuses it.
        with pytest.raises(RiskViolation, match="max_position"):
            await asyncio.wait_for(task, 8)
    assert btc_position(wire) == D("0.01")
    assert not wire.orders()
    assert json.loads(risk_state_path(selected, tmp_path).read_text())["execution"] is None


async def test_halt_past_the_halt_budget_latches_the_unconfirmed_cancels(tmp_path):
    wire, selected = HaltingWire(), halting_config("1", max_engine_halt_seconds=D("1.5"))
    async with episode(wire, selected, tmp_path) as (task, _):
        await until(wire, lambda: len(wire.orders()) == 4)
        originals = set(wire.orders())
        wire.halted = True
        wire.socket.queue.put_nowait(SAFETY)
        halted_at = time.monotonic()
        with pytest.raises(RiskViolation, match="cancel_outcome_unknown"):
            await asyncio.wait_for(task, 8)
        assert time.monotonic() - halted_at >= 1.5
    state = json.loads(risk_state_path(selected, tmp_path).read_text())
    assert state["execution"]["reason"] == "cancel_outcome_unknown"
    assert {row["order_id"] for row in state["execution"]["originals"]} <= originals


async def test_reconnects_during_a_halt_wait_past_the_stale_window(tmp_path):
    """A halt reported while reconnecting is not counted against max_stale_pause_seconds."""
    wire, selected, lines = HaltingWire(), halting_config("1"), []
    wire.refuse_http = False  # Cancels still go through: only the stream and health report it.
    async with episode(wire, selected, tmp_path, report=lines.append) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        wire.halted = True
        wire.socket.queue.put_nowait(SAFETY)
        await run_until(task, lambda: not wire.orders() and wire.connections >= 3, seconds=3)
        await asyncio.sleep(1.5)  # Every reconnect is refused by the halt past the window.
        assert not task.done(), "the bot gave up reconnecting during the engine halt"
        wire.halted = False
        await run_until(task, lambda: len(wire.orders()) == 4, seconds=8)
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()
    assert not any("reconnect window" in line for line in lines)


@pytest.mark.parametrize(
    ("status", "body", "ready"),
    [
        (200, {"state": "READY", "reason": None}, True),
        (503, HALTED_HEALTH, False),
        (503, {"state": "PROJECTION_UNAVAILABLE", "reason": "ENGINE_NOT_READY"}, False),
        (503, {"unexpected": True}, None),
        (502, {"state": "READY"}, None),
    ],
)
async def test_engine_ready_reads_public_health(status, body, ready):
    def handler(request):
        assert request.method == "GET" and request.url.path == "/v1/health"
        assert "X-Glyde-API-Key" not in request.headers
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OneApiClient(live_config(), http_client=http)
        assert await client.engine_ready() is ready


async def test_unreachable_health_is_unknown():
    def handler(request):
        raise httpx.ConnectError("synthetic", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await OneApiClient(live_config(), http_client=http).engine_ready() is None


def test_engine_unavailable_refusals_are_distinguished_from_drops():
    assert engine_unavailable(SubscriptionDeferred("ENGINE_NOT_READY", 5))
    assert engine_unavailable(SubscriptionDeferred("ADAPTER_INTEGRITY_CONFLICT", 1))
    assert engine_unavailable(OneApiHTTPError(503, "PROJECTION_UNAVAILABLE"))
    assert engine_unavailable(EngineUnavailable("refused"))
    assert not engine_unavailable(OneApiHTTPError(503))
    assert not engine_unavailable(SubscriptionDeferred("SOMETHING_ELSE", 1))
    assert not engine_unavailable(StreamDisconnected("WebSocket disconnected"))
    assert not engine_unavailable(None)


async def test_info_refusal_keeps_its_reason_code_and_trading_unavailable_is_classified():
    def handler(request):
        if request.url.path.endswith("/info"):
            return httpx.Response(503, json=PROJECTION_REFUSAL)
        return httpx.Response(
            200, json={"status": "err", "response": "Glyde: trading is unavailable"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OneApiClient(live_config(), http_client=http)
        with pytest.raises(OneApiHTTPError) as refused:
            await client.open_orders()
        assert refused.value.reason_code == "PROJECTION_UNAVAILABLE"
        with pytest.raises(EngineUnavailable):
            OneApiClient._statuses(
                {"status": "err", "response": "Glyde: trading is unavailable"}, 1
            )


async def test_starting_during_a_halt_waits_for_glyde_instead_of_exiting(tmp_path):
    """Production 26 Nov 2026: a restart during a halt ended at once with HTTP 503."""
    wire, selected, lines = HaltingWire(), halting_config("1"), []
    wire.halted = True
    async with episode(wire, selected, tmp_path, report=lines.append) as (task, stop):
        await run_until(task, lambda: wire.connections >= 2 or wire.changed.is_set(), seconds=3)
        await asyncio.sleep(1.5)  # Past max_stale_pause_seconds, still halted.
        assert not task.done(), "the bot exited instead of waiting out the halt"
        assert not wire.orders()
        wire.halted = False
        await run_until(task, lambda: len(wire.orders()) == 4, seconds=8)
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()
