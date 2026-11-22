"""Bounded writes, durable dispatch fences and conservative restart behavior."""

import asyncio
import json
import threading
from dataclasses import replace
from decimal import Decimal as D

import pytest
from test_risk import Account, FakeClient, Order, guard, guard_restart

from glyde_mm.models import BOT_CLOID_PREFIX, HistoricalOrder, OpenOrder, bot_client_order_id
from glyde_mm.risk import RiskManagedClient, RiskViolation, VenueSweepDeferred
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


@pytest.mark.parametrize("missing", ["execution", "order_attempts"])
async def test_checkpoint_missing_current_fields_refuses_without_rewriting(tmp_path, missing):
    path = tmp_path / "risk.json"
    initial, _ = guard(path)
    await initial.close_state()
    checkpoint = json.loads(path.read_text())
    del checkpoint[missing]
    original = json.dumps(checkpoint)
    path.write_text(original)
    with pytest.raises(RiskViolation, match="risk_state_unavailable"):
        guard(path)
    assert path.read_text() == original


def test_obsolete_risk_state_path_config_is_rejected(tmp_path):
    from glyde_mm.config import load_config

    path = tmp_path / "bot.yaml"
    path.write_text("mode: paper\nrisk_state_path: ignored.json\n")
    with pytest.raises(ValueError, match="unknown configuration field"):
        load_config(path)


class ProjectionOutageClient(FakeClient):
    """Production 22 Nov 2026: the reconcile sweep waited for an owner cut that never came."""

    async def cancel_all(self, symbol=None):
        self.all_cancels += 1
        raise TimeoutError("owner snapshot withheld")


async def test_transient_sweep_failure_with_nothing_outstanding_defers_without_a_latch(tmp_path):
    path = tmp_path / "risk.json"
    g, now = guard(path)
    managed = RiskManagedClient(ProjectionOutageClient(), g)
    with pytest.raises(VenueSweepDeferred):
        await managed.cancel_all("BTC")
    await g.flush_state()
    assert g.halted is None
    assert json.loads(path.read_text())["execution"] is None
    await g.close_state()
    restarted, _ = guard_restart(path, now)
    assert restarted.halted is None
    await restarted.close_state()


async def test_transient_sweep_failure_after_confirmed_cancels_clears_its_originals(tmp_path):
    path = tmp_path / "risk.json"
    g, _ = guard(path)
    managed = RiskManagedClient(ProjectionOutageClient(), g)
    await managed.place_orders([Order(client_order_id=bot_client_order_id())])
    assert g.pending
    with pytest.raises(VenueSweepDeferred):
        await managed.cancel_all("BTC")
    await g.flush_state()
    assert not g.pending and g.halted is None
    assert json.loads(path.read_text())["execution"] is None
    await g.close_state()


async def test_unconfirmed_cancel_keeps_its_latch_despite_a_transient_sweep(tmp_path):
    path = tmp_path / "risk.json"
    g, _ = guard(path)
    client = ProjectionOutageClient()
    managed = RiskManagedClient(client, g)
    await managed.place_orders([Order(client_order_id=bot_client_order_id())])
    client.cancel_result = "rejected"
    with pytest.raises(RiskViolation, match="cancel_outcome_unknown"):
        await managed.cancel_all("BTC")
    await g.flush_state()
    assert len(json.loads(path.read_text())["execution"]["originals"]) == 1
    await g.close_state()


def latched(path, originals, *, loss_halted=False):
    path.write_text(
        json.dumps(
            {
                "day": "2023-11-14",
                "equity_start": "100",
                "loss_halted": loss_halted,
                "execution": {"reason": "cancel_outcome_unknown", "originals": originals},
                "order_attempts": [],
            }
        )
    )


def test_empty_cancel_latch_is_never_written(tmp_path):
    path = tmp_path / "risk.json"
    g, _ = guard(path)
    assert g.begin_execution("cancel_outcome_unknown", []) is False
    asyncio.run(g.close_state())
    assert json.loads(path.read_text())["execution"] is None


def test_empty_cancel_latch_on_disk_does_not_block_startup(tmp_path):
    path = tmp_path / "risk.json"
    latched(path, [])
    g, _ = guard(path)
    assert g.halted is None and g.execution_cancellations(None) == ()


ORIGINAL = {"symbol": "BTC", "order_id": "84", "client_order_id": None}
TAGGED = {"symbol": "BTC", "order_id": None, "client_order_id": BOT_CLOID_PREFIX + "ab" * 12}


def open_order(oid, cloid=None):
    return OpenOrder(oid, "BTC", "buy", D("0.0002"), D("60000"), cloid)


@pytest.mark.parametrize(
    ("open_orders", "history", "resolved"),
    [
        ([], [HistoricalOrder("84", "BTC", "canceled")], True),
        ([], [HistoricalOrder("84", "BTC", "filled"), HistoricalOrder("85", "BTC", "open")], True),
        ([], [HistoricalOrder("84", "BTC", "open")], False),
        ([], [HistoricalOrder("84", "ETH", "canceled")], False),
        ([], [HistoricalOrder("99", "BTC", "canceled")], False),
        ([], [], False),
        ([open_order("84")], [HistoricalOrder("84", "BTC", "canceled")], False),
    ],
)
def test_startup_resolves_a_cancel_latch_only_with_venue_proof(
    tmp_path, open_orders, history, resolved
):
    path = tmp_path / "risk.json"
    latched(path, [ORIGINAL])
    g, _ = guard_restart(path, [100.0, 1_700_000_000.0])
    assert g.halted == "cancel_outcome_unknown"
    assert g.resolve_cancel_latch(open_orders, history) is resolved
    assert g.halted == (None if resolved else "cancel_outcome_unknown")
    asyncio.run(g.close_state())
    assert (json.loads(path.read_text())["execution"] is None) is resolved


def test_cloid_only_original_resolves_by_cloid_and_open_cloid_keeps_it(tmp_path):
    path = tmp_path / "risk.json"
    latched(path, [ORIGINAL, TAGGED])
    g, _ = guard_restart(path, [100.0, 1_700_000_000.0])
    history = [
        HistoricalOrder("84", "BTC", "canceled"),
        HistoricalOrder("86", "BTC", "canceled", TAGGED["client_order_id"].upper()),
    ]
    assert not g.resolve_cancel_latch([open_order("86", TAGGED["client_order_id"])], history)
    assert g.resolve_cancel_latch([], history)


def test_resolved_cancel_latch_keeps_a_daily_loss_halt(tmp_path):
    path = tmp_path / "risk.json"
    latched(path, [ORIGINAL], loss_halted=True)
    g, _ = guard_restart(path, [100.0, 1_700_000_000.0])
    assert g.resolve_cancel_latch([], [HistoricalOrder("84", "BTC", "canceled")])
    assert g.halted == "daily_loss"
    asyncio.run(g.close_state())
    assert json.loads(path.read_text())["loss_halted"] is True
