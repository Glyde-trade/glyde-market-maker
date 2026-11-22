"""A stale book while connected pauses its market and resumes on fresh data, within a bound."""

import asyncio
import json
import socket
import time
from contextlib import asynccontextmanager
from decimal import Decimal as D

import httpx
import pytest
from live_fake import SequencedFake
from market_receipt import approved_market
from test_stream_reconnect import Paper, Venue, events

from glyde_mm.client import OneApiClient
from glyde_mm.config import Config, RiskConfig
from glyde_mm.risk import RiskViolation
from glyde_mm.runner import run

OWNER = "0x" + "1" * 40


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("stale-pause tests cannot open network sockets")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "0x" + "3" * 64)
    monkeypatch.setenv("GLYDE_READ_API_KEY", "read-fixture")


def paper_config(max_pause="300"):
    return Config(
        mode="paper",
        symbol="BTC",
        base_url="https://offline.invalid/v1",
        strategy_config={"order_amount": "0.01", "bid_spread": "0.02", "ask_spread": "0.02"},
        risk=RiskConfig(
            max_position=D("0.1"),
            max_order_notional=D("25"),
            stale_book_seconds=D("0.3"),
            max_stale_pause_seconds=D(max_pause),
        ),
    )


async def until(task, predicate, seconds=3, tick=None):
    async with asyncio.timeout(seconds):
        while not predicate():
            if task.done():
                await task  # Surface the run's failure instead of a bare timeout.
                raise AssertionError("run ended before the expected state")
            if tick is not None:
                tick()
            await asyncio.sleep(0.05)


@asynccontextmanager
async def paper_session(directory, selected):
    venue, paper, stop, lines = Venue(), Paper(selected.paper_balance), asyncio.Event(), []
    async with httpx.AsyncClient(transport=venue.http_transport()) as http:
        source = OneApiClient(selected, http_client=http, connect_ws=venue.connect_ws)
        task = asyncio.create_task(
            run(
                selected,
                state_directory=directory,
                client_factory=lambda _: source,
                paper_factory=lambda _: paper,
                stop_event=stop,
                report=lines.append,
            )
        )
        try:
            yield venue, paper, task, stop, lines
        finally:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)


def frozen_source(venue):
    """Books keep arriving, but their source clock barely moves (the 2026-11-22 incident)."""

    def tick():
        venue.advance_time(1)
        venue.set_book("BTC", bids=[("99", "10")], asks=[("101", "10")])

    return tick


def fresh_source(venue):
    def tick():
        venue.now_ms = time.time_ns() // 1_000_000
        venue.set_book("BTC", bids=[("99", "10")], asks=[("101", "10")])

    return tick


async def test_paper_stale_source_pauses_without_placing_then_resumes(tmp_path):
    async with paper_session(tmp_path, paper_config()) as (venue, paper, task, stop, lines):
        await until(task, lambda: len(paper.open_before_place) == 1, tick=fresh_source(venue))
        await until(
            task,
            lambda: "PAPER BTC: quoting paused (stale BTC)" in lines,
            tick=frozen_source(venue),
        )
        assert await paper.open_orders() == []
        placed = len(paper.open_before_place)
        # Still connected and still stale: nothing new is placed and the run keeps going.
        deadline = time.monotonic() + 0.6
        await until(task, lambda: time.monotonic() > deadline, tick=frozen_source(venue))
        assert len(paper.open_before_place) == placed and not task.done()
        await until(task, lambda: len(paper.open_before_place) > placed, tick=fresh_source(venue))
        assert "PAPER BTC: quoting resumed" in lines
        assert paper.open_before_place[-1] == 0 and len(await paper.open_orders()) == 2
        stop.set()
        await asyncio.wait_for(task, 2)
    rows = events(tmp_path)
    names = [row["event"] for row in rows]
    stale = rows[names.index("book_stale")]
    fresh = rows[names.index("book_fresh")]
    assert (stale["market"], stale["reason"]) == (0, "stale_book_source")
    assert fresh["market"] == 0 and fresh["paused_ms"] >= 600
    assert names.index("book_stale") < names.index("book_fresh")
    assert rows[-1]["event"] == "run_ended" and rows[-1]["outcome"] == "stopped"
    assert await paper.open_orders() == []


async def test_stale_past_pause_bound_ends_run_with_stale_risk_stop(tmp_path):
    async with paper_session(tmp_path, paper_config(max_pause="0.5")) as (
        venue,
        paper,
        task,
        _,
        lines,
    ):
        await until(task, lambda: len(paper.open_before_place) == 1, tick=fresh_source(venue))
        tick = frozen_source(venue)
        async with asyncio.timeout(3):
            while not task.done():
                tick()
                await asyncio.sleep(0.05)
        with pytest.raises(RiskViolation, match="stale_book_source"):
            await task
    assert "PAPER BTC: quoting paused (stale BTC)" in lines
    assert "open_quotes=0 | risk_stop=stale_book_source" in lines[-1]
    rows = events(tmp_path)
    names = [row["event"] for row in rows]
    assert "book_fresh" not in names
    paused = rows[names.index("book_stale")]
    from datetime import datetime

    held = datetime.fromisoformat(rows[-1]["time"]) - datetime.fromisoformat(paused["time"])
    assert held.total_seconds() >= 0.5
    assert rows[-1]["event"] == "run_ended" and rows[-1]["outcome"] == "risk_stop"


class Wire(SequencedFake):
    def __init__(self):
        super().__init__()
        self.actions = []

    def http(self, request):
        envelope = json.loads(request.content)
        if "action" in envelope:
            self.actions.append(envelope["action"])
        return super().http(request)

    def orders(self):
        return {
            str(row["oid"]): row for row in self.ledger.info({"type": "openOrders", "user": OWNER})
        }


async def test_live_resume_reconciles_venue_orders_before_quoting(tmp_path):
    wire, stop, lines = Wire(), asyncio.Event(), []
    selected = Config(
        mode="live",
        symbol="BTC",
        owner=OWNER,
        vault_address="0x" + "4" * 40,
        strategy_config={"order_amount": "0.01", "order_refresh_time": 60},
        risk=RiskConfig(
            max_position=D(1),
            max_order_notional=D(25),
            stale_book_seconds=D("0.4"),
            account_age_seconds=D(5),
            max_orders_per_minute=600,
        ),
    )
    approved_market(selected, tmp_path)

    def owner_only():
        wire.ledger.now_ms = time.time_ns() // 1_000_000
        wire.emit_owner()

    def fresh():
        owner_only()
        wire.emit_book()

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire.http)) as http:
        source = OneApiClient(selected, http_client=http, connect_ws=wire.connect)
        task = asyncio.create_task(
            run(
                selected,
                state_directory=tmp_path,
                client_factory=lambda _: source,
                stop_event=stop,
                report=lines.append,
            )
        )
        try:
            await until(task, lambda: len(wire.orders()) == 2)
            originals = set(wire.orders())
            # The owner stream stays current; only the book stops.
            await until(task, lambda: not wire.orders(), tick=owner_only)
            assert "LIVE BTC: quoting paused (stale BTC)" in lines
            # Venue state the paused strategy never placed, e.g. from a lost receipt.
            stray = wire.ledger.exchange(
                {
                    "action": {
                        "type": "order",
                        "orders": [
                            {
                                "a": 0,
                                "b": True,
                                "p": "90",
                                "s": "0.01",
                                "r": False,
                                "t": {"limit": {"tif": "Gtc"}},
                            }
                        ],
                    }
                }
            )["response"]["data"]["statuses"][0]["resting"]["oid"]
            paused_at = len(wire.actions)
            deadline = time.monotonic() + 0.5
            await until(task, lambda: time.monotonic() > deadline, tick=owner_only)
            assert not any(a["type"] == "order" for a in wire.actions[paused_at:])
            await until(
                task,
                lambda: len(wire.orders()) == 2 and str(stray) not in wire.orders(),
                tick=fresh,
            )
            assert set(wire.orders()).isdisjoint(originals)
            assert "LIVE BTC: quoting resumed" in lines and not task.done()
            after = wire.actions[paused_at:]
            first_order = next(i for i, a in enumerate(after) if a["type"] == "order")
            reconciled = {
                row["o"]
                for a in after[:first_order]
                if a["type"] == "cancel"
                for row in a["cancels"]
            }
            assert stray in reconciled
            stop.set()
            await asyncio.wait_for(task, 2)
        finally:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
    assert not wire.orders()
    names = [row["event"] for row in events(tmp_path)]
    assert names.count("book_stale") == names.count("book_fresh") == 1
    assert "disconnected" not in names
