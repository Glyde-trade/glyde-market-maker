"""A degraded engine's malformed frames reconnect; kept quotes survive only a bounded drop."""

import asyncio
import contextlib
import socket
from dataclasses import replace
from decimal import Decimal as D

import pytest
from test_quote_resilience import CANCEL_BOUND_SECONDS, FreshWire, episode, until
from test_quote_resilience import config as live_config

from glyde_mm import runner
from glyde_mm.client import OneApiError, reconnectable
from glyde_mm.live_state import ResnapshotFault, StreamFault, stream_fault

DEFERRED = {
    "channel": "error",
    "data": "Glyde: projection unavailable",
    "reasonCode": "PROJECTION_UNAVAILABLE",
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("degraded-stream tests cannot open network sockets")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "0x" + "3" * 64)
    monkeypatch.setenv("GLYDE_READ_API_KEY", "read-fixture")
    monkeypatch.setattr(runner, "RECONNECT_INITIAL_SECONDS", 0.01)


def keeping(seconds):
    selected = live_config()
    return replace(
        selected,
        risk=replace(selected.risk, keep_quotes_on_disconnect_seconds=D(seconds)),
    )


@pytest.mark.parametrize(
    ("error", "resnapshot"),
    [
        (KeyError("glyde"), True),
        (ValueError("bad decimal"), True),
        (OneApiError("invalid live batch membership"), True),
        (OneApiError("live stream continuity gap"), True),
        (OneApiError("private stream owner mismatch"), False),
        (OneApiError("WebSocket subscription refused (UNSPECIFIED)"), False),
    ],
)
def test_only_identity_faults_and_final_refusals_stay_fatal(error, resnapshot):
    fault = stream_fault("invalid", error, channel="l2Book")
    assert isinstance(fault, StreamFault) and fault.cause is error
    assert isinstance(fault, ResnapshotFault) is resnapshot
    assert reconnectable(fault) is resnapshot


async def test_malformed_frame_reconnects_instead_of_ending_the_run(tmp_path):
    wire, selected = FreshWire(), live_config()
    async with episode(wire, selected, tmp_path) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        originals = set(wire.orders())
        wire.socket.queue.put_nowait({"channel": "l2Book", "data": {}})
        await until(wire, lambda: wire.connections == 2 and len(wire.orders()) == 4)
        assert set(wire.orders()).isdisjoint(originals)
        assert not task.done()
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()


async def test_kept_quotes_rest_through_a_short_drop_then_reconcile(tmp_path):
    wire, selected = FreshWire(), keeping("5")
    async with episode(wire, selected, tmp_path) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        originals = set(wire.orders())
        wire.socket.queue.put_nowait({**DEFERRED, "retryAfter": 1})
        await asyncio.sleep(0.5)
        assert set(wire.orders()) == originals and wire.connections == 1
        await until(wire, lambda: wire.connections == 2 and len(wire.orders()) == 4)
        await until(wire, lambda: set(wire.orders()).isdisjoint(originals))
        assert not task.done()
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()
    wire.assert_no_duplicate_cancels()


async def test_kept_quotes_are_cancelled_once_the_drop_outlasts_the_bound(tmp_path):
    wire, selected = FreshWire(), keeping("0.3")
    async with episode(wire, selected, tmp_path) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        wire.socket.queue.put_nowait({**DEFERRED, "retryAfter": 3})
        await until(wire, lambda: not wire.orders())
        assert wire.connections == 1  # Swept while still waiting to reconnect.
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    wire.assert_no_duplicate_cancels()


async def test_heartbeat_pings_the_systemd_notify_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WATCHDOG_PING_SECONDS", 0.05)
    path = str(tmp_path / "notify")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
        notify.bind(path)
        notify.setblocking(False)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        heartbeat = asyncio.create_task(runner._systemd_heartbeat())
        await asyncio.sleep(0.2)
        heartbeat.cancel()
        pings = []
        with contextlib.suppress(BlockingIOError):
            while True:
                pings.append(notify.recv(64))
    assert len(pings) >= 2 and set(pings) == {b"WATCHDOG=1"}
