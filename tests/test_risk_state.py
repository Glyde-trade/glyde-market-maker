"""Bounded writes, durable dispatch fences and conservative restart behavior."""

import asyncio
import json
import threading
from dataclasses import replace

import pytest
from test_risk import Account, FakeClient, Order, guard, guard_restart

from glyde_mm.risk import RiskManagedClient, RiskViolation
from glyde_mm.risk_state import RiskStateStore


async def wait_for_thread(event):
    assert await asyncio.to_thread(event.wait, 2)


async def test_one_inflight_and_one_coalesced_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    store = RiskStateStore(path)
    started, release, written = threading.Event(), threading.Event(), []
    original = store._write

    def held(snapshot):
        written.append(snapshot)
        if len(written) == 1:
            started.set()
            assert release.wait(3)
        original(snapshot)

    monkeypatch.setattr(store, "_write", held)
    try:
        store.submit('"first"')
        await wait_for_thread(started)
        for value in range(100):
            store.submit(json.dumps(value))
        with store._lock:
            assert store._pending[0] == "99"
        release.set()
        await store.flush()
        assert written == ['"first"', "99"]
        assert path.read_text() == "99"
    finally:
        release.set()
        await store.close()


async def test_writer_failure_reaches_barrier_and_refuses_later_snapshots(tmp_path, monkeypatch):
    store = RiskStateStore(tmp_path / "risk.json")

    def failed(_):
        raise OSError("injected state failure")

    monkeypatch.setattr(store, "_write", failed)
    store.submit("{}")
    with pytest.raises(OSError, match="injected state failure"):
        await store.flush()
    with pytest.raises(OSError, match="injected state failure"):
        store.submit("{}")
    with pytest.raises(OSError, match="injected state failure"):
        await store.close()


async def test_worker_start_failure_refuses_without_stranding_a_barrier(tmp_path, monkeypatch):
    store = RiskStateStore(tmp_path / "risk.json")

    def failed(*_):
        raise RuntimeError("cannot start writer")

    monkeypatch.setattr(store._worker, "submit", failed)
    with pytest.raises(OSError, match="writer unavailable"):
        store.submit("{}")
    with pytest.raises(OSError, match="writer unavailable"):
        await asyncio.wait_for(store.close(), 1)


async def test_held_clear_does_not_delay_reply_and_restart_retains_original(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    g, now = guard(path)
    await g.flush_state()
    store = g._state_store
    started, release = threading.Event(), threading.Event()
    original = store._write

    def hold_clear(snapshot):
        state = json.loads(snapshot)
        if state["execution"] is None and state["order_attempts"] and not started.is_set():
            started.set()
            assert release.wait(3)
        original(snapshot)

    monkeypatch.setattr(store, "_write", hold_clear)
    managed = RiskManagedClient(FakeClient(), g)
    try:
        await asyncio.wait_for(managed.place_orders([Order(client_order_id="original")]), 2)
        await wait_for_thread(started)
        restored, _ = guard_restart(path, now)
        assert restored.halted == "order_outcome_unknown"
        assert restored.execution_cancellations(None)[0].client_order_id == "original"
        await restored.close_state()
        release.set()
        await g.flush_state()
        restored, _ = guard_restart(path, now)
        assert restored.halted is None
        await restored.close_state()
    finally:
        release.set()
        await managed.close()


@pytest.mark.parametrize("change", ["position", "stop", "book"])
async def test_durable_gate_rechecks_latest_risk_and_stop_before_dispatch(
    tmp_path, monkeypatch, change
):
    path = tmp_path / "risk.json"
    g, _ = guard(path)
    await g.flush_state()
    store = g._state_store
    started, release = threading.Event(), threading.Event()
    original = store._write

    def hold_intent(snapshot):
        if json.loads(snapshot)["execution"] is not None and not started.is_set():
            started.set()
            assert release.wait(3)
        original(snapshot)

    monkeypatch.setattr(store, "_write", hold_intent)
    raw, stop = FakeClient(), asyncio.Event()
    managed = RiskManagedClient(raw, g, stop_event=stop)
    task = asyncio.create_task(managed.place_orders([Order(client_order_id="never-sent")]))
    try:
        await wait_for_thread(started)
        assert not raw.placed
        if change == "position":
            raw.current_account = Account(positions={"BTC": g.limits.max_position})
        elif change == "stop":
            stop.set()
        else:
            g.update_book(replace(g.books["BTC"], received_at=90))
        release.set()
        with pytest.raises(RiskViolation, match="max_position|client_closed|stale_book"):
            await task
        assert not raw.placed
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await managed.close()
