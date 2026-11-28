"""Offline real-client runner safety against an independent matching ledger."""

import asyncio
import json
import socket
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal as D

import httpx
import pytest
from market_receipt import approved_market
from test_multi_market_live import MarketSocket, MarketWire
from test_stream_reconnect import until as run_until

from glyde_mm import runner
from glyde_mm.client import OneApiClient, ReadRotation
from glyde_mm.config import Config, MarketConfig, RiskConfig
from glyde_mm.models import BOT_CLOID_PREFIX, bot_client_order_id, is_bot_order
from glyde_mm.risk import RiskViolation
from glyde_mm.runner import risk_state_path, run

OWNER = "0x" + "1" * 40
CANCEL_BOUND_SECONDS = 2


class ResilienceWire(MarketWire):
    """Each connection installs a fresh baseline over the SAME retained venue ledger."""

    def __init__(self):
        super().__init__()
        self.actions = []
        self.lose_cancel_receipts = False
        self.refuse_history = False
        self.changed = asyncio.Event()

    def connect(self, url, **kwargs):
        self.sequence = self.book_sequence = 0
        self.books.clear()
        return super().connect(url, **kwargs)

    def http(self, request):
        envelope = json.loads(request.content)
        if "action" in envelope:
            self.actions.append((time.monotonic(), envelope["action"]))
        if self.refuse_history and envelope.get("type") == "historicalOrders":
            return httpx.Response(400, text="Glyde: unsupported request")
        response = super().http(request)
        self.changed.set()
        if self.lose_cancel_receipts and envelope.get("action", {}).get("type") == "cancel":
            raise httpx.ReadTimeout("synthetic lost cancel response", request=request)
        return response

    def orders(self, symbol=None):
        return {
            str(row["oid"]): row
            for row in self.ledger.info({"type": "openOrders", "user": OWNER})
            if symbol is None or row["coin"] == symbol
        }

    def refresh(self, *symbols):
        self.ledger.now_ms = time.time_ns() // 1_000_000
        for symbol in symbols:
            self.emit_book(symbol)
        self.emit_owner()

    def assert_no_duplicate_cancels(self):
        cancelled = [
            (row["a"], row["o"])
            for _, action in self.actions
            if action["type"] == "cancel"
            for row in action["cancels"]
        ]
        assert cancelled and all(count == 1 for count in Counter(cancelled).values())


def config():
    return Config(
        mode="live",
        owner=OWNER,
        vault_address="0x" + "4" * 40,
        markets=tuple(
            MarketConfig(symbol, D(1), {"order_amount": amount, "order_refresh_time": 60})
            for symbol, amount in (("BTC", "0.01"), ("ETH", "0.1"))
        ),
        risk=RiskConfig(
            max_position=D(1),
            max_order_notional=D(25),
            max_total_notional=D(50),
            stale_book_seconds=D("0.4"),
            account_age_seconds=D(5),
            max_orders_per_minute=600,
        ),
    )


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("resilience qualification cannot open network sockets")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "0x" + "3" * 64)
    monkeypatch.setenv("GLYDE_READ_API_KEY", "read-fixture")


async def until(wire, predicate):
    async with asyncio.timeout(CANCEL_BOUND_SECONDS):
        while True:
            wire.changed.clear()
            if predicate():
                return
            await wire.changed.wait()


@asynccontextmanager
async def episode(wire, selected, directory, **options):
    stop = asyncio.Event()
    approved_market(selected, directory)  # This test isolates post-preflight lifecycle.
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire.http)) as http:
        source = OneApiClient(selected, http_client=http, connect_ws=wire.connect)
        task = asyncio.create_task(
            run(
                selected,
                state_directory=directory,
                client_factory=lambda _: source,
                stop_event=stop,
                **{"report": lambda _: None, **options},
            )
        )
        try:
            yield task, stop
        finally:
            stop.set()
            if not task.done():
                await asyncio.wait_for(asyncio.shield(task), CANCEL_BOUND_SECONDS)
            else:
                # Always retrieve expected failure; individual cases assert its exact scope.
                await asyncio.gather(task, return_exceptions=True)


async def test_gap_cancels_once_and_reconnects_from_a_fresh_baseline(tmp_path):
    """A sequencing gap (e.g. Glyde's engine restarting on a new epoch) retires quotes and
    resubscribes, like a socket drop; it no longer ends the run (26 Nov 2026)."""
    wire, selected, lines = FreshWire(), config(), []
    async with episode(wire, selected, tmp_path, report=lines.append) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        originals = set(wire.orders())
        start = time.monotonic()
        wire.emit_owner(skip=1)
        await until(wire, lambda: not set(wire.orders()) & originals)
        assert time.monotonic() - start < CANCEL_BOUND_SECONDS
        await until(wire, lambda: len(wire.orders()) == 4 and wire.connections == 2)
        assert set(wire.orders()).isdisjoint(originals) and not task.done()
        wire.assert_no_duplicate_cancels()
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()
    wire.assert_no_duplicate_cancels()
    # Confirmed cancellation is not an unknown result merely because the read WS gapped.
    assert json.loads(risk_state_path(selected, tmp_path).read_text())["execution"] is None
    assert any("stream disconnected" in line and "reconnecting" in line for line in lines)


class FreshWire(ResilienceWire):
    def connect(self, url, **kwargs):
        self.ledger.now_ms = time.time_ns() // 1_000_000  # A new socket's snapshot is current.
        return super().connect(url, **kwargs)


async def test_socket_drop_reconciles_venue_orders_before_requoting(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RECONNECT_INITIAL_SECONDS", 0.2)
    wire, selected = FreshWire(), config()
    async with episode(wire, selected, tmp_path) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        originals = set(wire.orders())
        await wire.socket.close()
        await until(wire, lambda: not wire.orders())
        dropped = len(wire.actions)

        def rest(price, **tag):
            wire_order = {"a": 0, "b": True, "p": price, "s": "0.01", "r": False}
            wire_order |= {"t": {"limit": {"tif": "Gtc"}}, **tag}
            action = {"type": "order", "orders": [wire_order]}
            return str(
                wire.ledger.exchange({"action": action})["response"]["data"]["statuses"][0][
                    "resting"
                ]["oid"]
            )

        # A bot order the dead session cannot see, e.g. from a lost receipt.
        stray = rest("90", c=bot_client_order_id())
        # The account holder's own manual order: never the bot's to cancel.
        manual = rest("80")
        await until(
            wire,
            lambda: len(wire.orders()) == 5 and stray not in wire.orders(),
        )
        assert manual in wire.orders()
        assert set(wire.orders()).isdisjoint(originals)
        assert wire.connections == 2 and not task.done()
        after = [action for _, action in wire.actions[dropped:]]
        first_order = next(i for i, action in enumerate(after) if action["type"] == "order")
        reconciled = {
            row["o"]
            for action in after[:first_order]
            if action["type"] == "cancel"
            for row in action["cancels"]
        }
        assert int(stray) in reconciled
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert set(wire.orders()) == {manual}
    cancelled = {row["o"] for _, a in wire.actions if a["type"] == "cancel" for row in a["cancels"]}
    assert int(manual) not in cancelled
    wire.assert_no_duplicate_cancels()
    state = json.loads(risk_state_path(selected, tmp_path).read_text())
    assert state["execution"] is None


async def test_stale_books_cancel_within_watchdog_bound_and_restart_cleanly(tmp_path):
    wire, selected = ResilienceWire(), config()
    async with episode(wire, selected, tmp_path) as (task, _):
        await until(wire, lambda: len(wire.orders()) == 4)
        original = set(wire.orders())
        start = time.monotonic()
        await until(wire, lambda: not wire.orders())
        assert time.monotonic() - start < CANCEL_BOUND_SECONDS
        # Multi-market staleness pauses rather than ending the shared process.
        assert not task.done()
        wire.refresh("BTC", "ETH")
        await until(wire, lambda: len(wire.orders()) == 4)
        assert set(wire.orders()).isdisjoint(original)
    assert not wire.orders()
    wire.assert_no_duplicate_cancels()


async def test_each_stale_market_retires_without_cancelling_fresh_peer_and_can_flip(tmp_path):
    wire, selected = ResilienceWire(), config()
    async with episode(wire, selected, tmp_path) as (task, _):
        await until(wire, lambda: len(wire.orders()) == 4)
        for stale, fresh in (("ETH", "BTC"), ("BTC", "ETH"), ("ETH", "BTC")):
            previous_stale, previous_fresh = set(wire.orders(stale)), set(wire.orders(fresh))
            start = time.monotonic()
            async with asyncio.timeout(CANCEL_BOUND_SECONDS):
                while wire.orders(stale):
                    wire.refresh(fresh)
                    await asyncio.sleep(0.02)
            assert time.monotonic() - start < CANCEL_BOUND_SECONDS
            assert set(wire.orders(fresh)) == previous_fresh
            assert not task.done()
            wire.refresh("BTC", "ETH")
            await until(wire, lambda stale=stale: len(wire.orders(stale)) == 2)
            assert set(wire.orders(stale)).isdisjoint(previous_stale)
    assert not wire.orders()
    wire.assert_no_duplicate_cancels()


@pytest.mark.parametrize("interrupted", [False, True])
async def test_new_process_objects_reuse_durable_state_without_orphan_or_duplicate(
    tmp_path, interrupted
):
    wire, selected = ResilienceWire(), config()
    async with episode(wire, selected, tmp_path) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        originals = set(wire.orders())
        if interrupted:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            stop.set()
            await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()
    state = json.loads(risk_state_path(selected, tmp_path).read_text())
    assert state["execution"] is None
    wire.ledger.now_ms = time.time_ns() // 1_000_000
    async with episode(wire, selected, tmp_path) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        assert set(wire.orders()).isdisjoint(originals)
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders() and wire.connections == 2
    wire.assert_no_duplicate_cancels()


async def lose_cancel_receipts(wire, selected, directory):
    # Without order history the in-run proof is retried for the (short) pause window, then latched.
    selected = replace(selected, risk=replace(selected.risk, max_stale_pause_seconds=D("0.3")))
    async with episode(wire, selected, directory) as (task, _):
        await until(wire, lambda: len(wire.orders()) == 4)
        originals = set(wire.orders())
        wire.lose_cancel_receipts = wire.refuse_history = True
        await wire.socket.close()
        with pytest.raises(RiskViolation, match="cancel_outcome_unknown"):
            await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    wire.refuse_history = False
    assert not wire.orders()  # Ledger applied cancel; the bot has no confirming receipts.
    state = json.loads(risk_state_path(selected, directory).read_text())
    assert state["execution"]["reason"] == "cancel_outcome_unknown"
    assert {row["order_id"] for row in state["execution"]["originals"]} == originals
    wire.lose_cancel_receipts = False
    wire.ledger.now_ms = time.time_ns() // 1_000_000
    return originals


async def test_lost_cancel_receipt_keeps_originals_fenced_without_venue_history(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runner, "RECONNECT_INITIAL_SECONDS", 0.1)
    wire, selected, lines = FreshWire(), config(), []
    await lose_cancel_receipts(wire, selected, tmp_path)
    placements = sum(action["type"] == "order" for _, action in wire.actions)
    wire.refuse_history = True
    # Startup retries the proof for the reconnect window, then stops with the latch kept.
    selected = replace(selected, risk=replace(selected.risk, max_stale_pause_seconds=D("0.5")))
    async with episode(wire, selected, tmp_path, report=lines.append) as (task, _):
        with pytest.raises(RiskViolation, match="cancel_outcome_unknown"):
            await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert sum(action["type"] == "order" for _, action in wire.actions) == placements
    assert json.loads(risk_state_path(selected, tmp_path).read_text())["execution"] is not None
    assert any("cancel_outcome_unknown kept past" in line for line in lines)


async def test_startup_cancel_latch_retries_until_venue_history_returns(tmp_path, monkeypatch):
    # 28 Nov 14:37: a restart found the latch before history could prove the cancels, and
    # exited for systemd to restart. It now retries inside the reconnect window.
    monkeypatch.setattr(runner, "RECONNECT_INITIAL_SECONDS", 0.1)
    wire, selected, lines = FreshWire(), config(), []
    originals = await lose_cancel_receipts(wire, selected, tmp_path)
    wire.refuse_history = True
    asyncio.get_running_loop().call_later(0.5, setattr, wire, "refuse_history", False)
    async with episode(wire, selected, tmp_path, report=lines.append) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        assert set(wire.orders()).isdisjoint(originals) and not task.done()
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert any("cancel_outcome_unknown unresolved" in line for line in lines)
    assert any("cancel_outcome_unknown resolved" in line for line in lines)


async def test_lost_cancel_receipt_resolves_at_startup_when_venue_history_proves_closed(tmp_path):
    wire, selected, lines = FreshWire(), config(), []
    originals = await lose_cancel_receipts(wire, selected, tmp_path)
    async with episode(wire, selected, tmp_path, report=lines.append) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        assert set(wire.orders()).isdisjoint(originals) and not task.done()
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()
    assert json.loads(risk_state_path(selected, tmp_path).read_text())["execution"] is None
    assert any("cancel_outcome_unknown resolved" in line for line in lines)


async def test_manual_order_survives_live_start_quoting_and_shutdown(tmp_path):
    wire, selected = ResilienceWire(), config()
    wire.ledger.now_ms = time.time_ns() // 1_000_000
    manual_wire = {"a": 0, "b": True, "p": "80", "s": "0.01", "r": False}
    manual_wire["t"] = {"limit": {"tif": "Gtc"}}
    receipt = wire.ledger.exchange({"action": {"type": "order", "orders": [manual_wire]}})
    manual = str(receipt["response"]["data"]["statuses"][0]["resting"]["oid"])
    async with episode(wire, selected, tmp_path, allow_existing_exposure=True) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 5)
        assert manual in wire.orders() and not task.done()
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert set(wire.orders()) == {manual}
    placed = [row for _, a in wire.actions if a["type"] == "order" for row in a["orders"]]
    assert len(placed) >= 4 and all(is_bot_order(row["c"]) for row in placed)
    assert all(row["c"].startswith(BOT_CLOID_PREFIX) and len(row["c"]) == 34 for row in placed)
    cancels = [a for _, a in wire.actions if a["type"] in {"cancel", "cancelByCloid"}]
    assert cancels and all(row.get("o") != int(manual) for a in cancels for row in a["cancels"])


async def test_stale_account_pulls_quotes_and_reconnects_instead_of_stopping(tmp_path, monkeypatch):
    # GLYDE-159: account resnapshots stalled 40-120s while books stayed fresh, and the bot
    # exited on stale_account (28 Nov 2026). It now retires its quotes and resnapshots.
    monkeypatch.setattr(runner, "RECONNECT_INITIAL_SECONDS", 0.2)
    wire = FreshWire()
    selected = replace(config(), risk=replace(config().risk, account_age_seconds=D(1)))
    owner_fresh = [True]

    async def feed():
        while True:
            wire.ledger.now_ms = time.time_ns() // 1_000_000
            wire.emit_book("BTC")
            wire.emit_book("ETH")
            if owner_fresh[0]:
                wire.emit_owner()
            await asyncio.sleep(0.1)

    async with episode(wire, selected, tmp_path) as (task, _):
        await until(wire, lambda: len(wire.orders()) == 4)
        original = set(wire.orders())
        feeder = asyncio.create_task(feed())
        try:
            owner_fresh[0] = False
            await until(wire, lambda: not wire.orders())
            assert not task.done()
            owner_fresh[0] = True
            await until(wire, lambda: len(wire.orders()) == 4)
            assert set(wire.orders()).isdisjoint(original) and not task.done()
            assert wire.connections >= 2
        finally:
            feeder.cancel()
    wire.assert_no_duplicate_cancels()


class RotatingSocket(MarketSocket):
    """Raises the client's planned read rotation when the test queues ROTATE."""

    async def __anext__(self):
        frame = await self.queue.get()
        if frame == "ROTATE":
            raise ReadRotation("read credential rotation")
        if frame is None:
            raise StopAsyncIteration
        return json.dumps(frame)


class RotatingWire(FreshWire):
    def connect(self, url, **kwargs):
        super().connect(url, **kwargs)
        return RotatingSocket(self)


async def test_read_rotation_keeps_resting_quotes_and_is_not_a_fault(tmp_path, monkeypatch):
    # #122: every planned read rotation (~4.5 min) retired and re-placed the whole ladder.
    monkeypatch.setattr(runner, "RECONNECT_INITIAL_SECONDS", 0.05)
    wire, selected, lines = RotatingWire(), config(), []

    async def feed():
        while True:
            wire.refresh("BTC", "ETH")
            await asyncio.sleep(0.1)

    async with episode(wire, selected, tmp_path, report=lines.append) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        feeder = asyncio.create_task(feed())
        try:
            original, before = set(wire.orders()), len(wire.actions)
            wire.socket.queue.put_nowait("ROTATE")
            # An adopted rotation sends no HTTP, so poll the run instead of the wire.
            await run_until(task, lambda: wire.connections == 2)
            await asyncio.sleep(0.5)
            assert set(wire.orders()) == original and not task.done()
            assert len(wire.actions) == before  # Nothing cancelled or re-placed.
            assert not any("stream disconnected" in line for line in lines)
            stop.set()
            await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
        finally:
            feeder.cancel()
    assert not wire.orders()
    events = [
        json.loads(row)["event"]
        for path in tmp_path.rglob("events.jsonl")
        for row in path.read_text().splitlines()
    ]
    assert "read_rotation" in events and "disconnected" not in events
