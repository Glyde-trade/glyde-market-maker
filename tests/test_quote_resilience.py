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
from test_multi_market_live import MarketWire

from glyde_mm import runner
from glyde_mm.client import OneApiClient
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


async def test_gap_cancels_once_and_restart_uses_fresh_baseline(tmp_path):
    wire, selected = ResilienceWire(), config()
    async with episode(wire, selected, tmp_path) as (task, _):
        await until(wire, lambda: len(wire.orders()) == 4)
        originals = set(wire.orders())
        start = time.monotonic()
        wire.emit_owner(skip=1)
        await until(wire, lambda: not wire.orders())
        assert time.monotonic() - start < CANCEL_BOUND_SECONDS
        with pytest.raises(Exception, match="disconnected|stream"):
            await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    wire.assert_no_duplicate_cancels()
    # Confirmed cancellation is not an unknown result merely because the read WS is gone.
    state = json.loads(risk_state_path(selected, tmp_path).read_text())
    assert state["execution"] is None
    attempts = len(state["order_attempts"])
    wire.ledger.now_ms = time.time_ns() // 1_000_000
    async with episode(wire, selected, tmp_path) as (task, stop):
        await until(wire, lambda: len(wire.orders()) == 4)
        assert set(wire.orders()).isdisjoint(originals)
        assert not task.done() and wire.connections == 2
        stop.set()
        await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert not wire.orders()
    wire.assert_no_duplicate_cancels()
    restarted = json.loads(risk_state_path(selected, tmp_path).read_text())
    assert len(restarted["order_attempts"]) > attempts  # Rate fence wasn't reset for restart.


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


async def test_lost_cancel_receipt_keeps_originals_fenced_without_venue_history(tmp_path):
    wire, selected, lines = FreshWire(), config(), []
    await lose_cancel_receipts(wire, selected, tmp_path)
    placements = sum(action["type"] == "order" for _, action in wire.actions)
    wire.refuse_history = True
    async with episode(wire, selected, tmp_path, report=lines.append) as (task, _):
        with pytest.raises(RiskViolation, match="cancel_outcome_unknown"):
            await asyncio.wait_for(task, CANCEL_BOUND_SECONDS)
    assert sum(action["type"] == "order" for _, action in wire.actions) == placements
    assert json.loads(risk_state_path(selected, tmp_path).read_text())["execution"] is not None
    assert any("cancel_outcome_unknown kept" in line for line in lines)


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
