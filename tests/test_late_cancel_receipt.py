"""A late cancel receipt or lagging owner stream pauses quoting; it never ends the run."""

import asyncio
import json
import socket
import time
from dataclasses import replace
from decimal import Decimal as D

import pytest
from test_quote_resilience import CANCEL_BOUND_SECONDS, FreshWire, episode, until
from test_quote_resilience import config as live_config
from test_stream_reconnect import until as run_until

from glyde_mm import runner
from glyde_mm.runner import risk_state_path


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("late-receipt tests cannot open network sockets")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "0x" + "3" * 64)
    monkeypatch.setenv("GLYDE_READ_API_KEY", "read-fixture")
    monkeypatch.setattr(runner, "RECONNECT_INITIAL_SECONDS", 0.05)


class DegradedOwnerWire(FreshWire):
    """Books keep flowing; owner cuts and cancel receipts can lag like production's degraded WS."""

    def __init__(self):
        super().__init__()
        self.owner_degraded = False
        self.cancelled_at = []

    async def feed(self):
        while True:
            self.ledger.now_ms = time.time_ns() // 1_000_000
            if self.socket is not None:
                self.emit_book("BTC")
                self.emit_book("ETH")
                if not self.owner_degraded:
                    self.emit_owner()
            await asyncio.sleep(0.05)

    def emit_owner(self, *, skip=0):
        if not self.owner_degraded:
            super().emit_owner(skip=skip)

    def http(self, request):
        if json.loads(request.content).get("action", {}).get("type") == "cancel":
            self.cancelled_at.append(time.monotonic())
        return super().http(request)


def refreshing(seconds="0.5", **risk):
    selected = live_config()
    markets = tuple(
        replace(m, strategy_config={**m.strategy_config, "order_refresh_time": float(seconds)})
        for m in selected.markets
    )
    return replace(selected, markets=markets, risk=replace(selected.risk, **risk))


async def test_refresh_cancel_while_owner_stream_lags_pauses_then_requotes(tmp_path):
    """Production 22 Nov 2026 14:52: receipts arrived, the owner cut confirming them did not."""
    wire, selected, lines = DegradedOwnerWire(), refreshing(), []
    feeder = asyncio.create_task(wire.feed())
    try:
        async with episode(wire, selected, tmp_path, report=lines.append) as (task, stop):
            await until(wire, lambda: len(wire.orders()) == 4)
            originals = set(wire.orders())
            wire.owner_degraded = True
            await run_until(task, lambda: not wire.orders(), seconds=3)
            await asyncio.sleep(2.5)  # Past the 2s cut deadline for the cancelled orders.
            assert not task.done()
            wire.owner_degraded = False
            await run_until(task, lambda: len(wire.orders()) == 4, seconds=6)
            assert set(wire.orders()).isdisjoint(originals)
            stop.set()
            await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    finally:
        feeder.cancel()
    assert not wire.orders()
    assert json.loads(risk_state_path(selected, tmp_path).read_text())["execution"] is None


async def test_lost_refresh_cancel_receipt_is_proven_closed_and_quoting_continues(tmp_path):
    wire, selected, lines = DegradedOwnerWire(), refreshing(), []
    feeder = asyncio.create_task(wire.feed())
    try:
        async with episode(wire, selected, tmp_path, report=lines.append) as (task, stop):
            await until(wire, lambda: len(wire.orders()) == 4)
            originals = set(wire.orders())
            wire.lose_cancel_receipts = True
            await run_until(task, lambda: not set(wire.orders()) & originals, seconds=3)
            wire.lose_cancel_receipts = False
            await run_until(task, lambda: len(wire.orders()) == 4, seconds=4)
            assert set(wire.orders()).isdisjoint(originals)
            assert wire.connections == 1 and not task.done()
            stop.set()
            await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    finally:
        feeder.cancel()
    assert any("late-receipt cancel(s) closed" in line for line in lines)
    assert json.loads(risk_state_path(selected, tmp_path).read_text())["execution"] is None


async def test_lost_cancel_receipt_without_proof_latches_after_the_pause_window(tmp_path):
    wire, selected = DegradedOwnerWire(), refreshing(max_stale_pause_seconds=D(1))
    feeder = asyncio.create_task(wire.feed())
    try:
        async with episode(wire, selected, tmp_path) as (task, _):
            await until(wire, lambda: len(wire.orders()) == 4)
            originals = set(wire.orders())
            wire.lose_cancel_receipts = wire.refuse_history = True
            with pytest.raises(Exception, match="cancel_outcome_unknown"):
                await asyncio.wait_for(task, 6)
            # Proof was retried for the whole pause window before latching.
            assert time.monotonic() - wire.cancelled_at[0] >= 1
    finally:
        feeder.cancel()
    state = json.loads(risk_state_path(selected, tmp_path).read_text())
    assert state["execution"]["reason"] == "cancel_outcome_unknown"
    assert {row["order_id"] for row in state["execution"]["originals"]} <= originals
