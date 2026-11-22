"""Paper socket drops reconnect from a fresh snapshot; stops and prior paper state never block."""

import asyncio
import json
import socket
import time
from contextlib import asynccontextmanager
from decimal import Decimal as D

import httpx
import pytest

from glyde_mm import runner
from glyde_mm.client import OneApiClient
from glyde_mm.config import Config, RiskConfig
from glyde_mm.paper import PaperExchange
from glyde_mm.runner import risk_state_path, run
from glyde_mm.testing import FakeOneApiServer


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("reconnect tests cannot open network sockets")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)


class Venue(FakeOneApiServer):
    def __init__(self):
        super().__init__(timestamp_ms=time.time_ns() // 1_000_000)
        self.set_book("BTC", bids=[("99", "10")], asks=[("101", "10")])
        self.sockets = []

    def connect_ws(self, url="", **kwargs):
        ws = super().connect_ws(url, **kwargs)
        self.sockets.append(ws)
        return ws


class Paper(PaperExchange):
    def __init__(self, balance):
        super().__init__(balance)
        self.open_before_place = []

    async def place_orders(self, orders):
        self.open_before_place.append(len(await self.open_orders()))
        return await super().place_orders(orders)


def config():
    return Config(
        mode="paper",
        symbol="BTC",
        base_url="https://offline.invalid/v1",
        strategy_config={"order_amount": "0.01", "bid_spread": "0.02", "ask_spread": "0.02"},
        risk=RiskConfig(max_position=D("0.1"), max_order_notional=D("25"), stale_book_seconds=D(5)),
    )


async def until(task, predicate, seconds=2):
    async with asyncio.timeout(seconds):
        while not predicate():
            if task.done():
                await task  # Surface the run's failure instead of a bare timeout.
                raise AssertionError("run ended before the expected state")
            await asyncio.sleep(0.01)


def events(directory):
    path = next(directory.glob("logs/run-*/events.jsonl"))
    return [json.loads(line) for line in path.read_text().splitlines()]


@asynccontextmanager
async def session(directory, selected):
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


async def test_paper_drop_reconnects_resubscribes_and_resumes_quoting(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RECONNECT_INITIAL_SECONDS", 0.05)
    async with session(tmp_path, config()) as (venue, paper, task, stop, _):
        await until(task, lambda: len(paper.open_before_place) == 1)
        venue.advance_time(1)
        await venue.disconnect()
        await until(task, lambda: len(paper.open_before_place) == 2)
        assert len(venue.sockets) == 2 and not task.done()
        assert venue.sockets[1].subscriptions == venue.sockets[0].subscriptions
        # Simulated quotes were retired while disconnected, before the fresh book quoted.
        assert paper.open_before_place == [0, 0]
        assert len(await paper.open_orders()) == 2
        stop.set()
        await asyncio.wait_for(task, 2)
    rows = events(tmp_path)
    names = [row["event"] for row in rows]
    assert names.count("disconnected") == names.count("reconnected") == 1
    assert names.index("disconnected") < names.index("reconnected")
    reconnected = rows[names.index("reconnected")]
    assert reconnected["attempts"] == 1 and reconnected["downtime_ms"] >= 0
    assert rows[-1]["event"] == "run_ended" and rows[-1]["outcome"] == "stopped"
    assert await paper.open_orders() == []


async def test_explicit_stop_during_backoff_exits_promptly(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RECONNECT_INITIAL_SECONDS", 60.0)
    async with session(tmp_path, config()) as (venue, paper, task, stop, lines):
        await until(task, lambda: len(paper.open_before_place) == 1)
        await venue.disconnect()
        await until(task, lambda: any("reconnecting" in line for line in lines))
        assert await paper.open_orders() == []
        started = time.monotonic()
        stop.set()
        await asyncio.wait_for(task, 1)
        assert time.monotonic() - started < 0.5
    assert len(venue.sockets) == 1
    assert events(tmp_path)[-1]["outcome"] == "stopped"


async def test_prior_paper_unknown_outcome_does_not_block_next_run(tmp_path):
    selected = config()
    path = risk_state_path(selected, tmp_path)
    originals = [
        {"symbol": "BTC", "order_id": None, "client_order_id": f"0x{i:032x}"} for i in range(4)
    ]
    path.write_text(
        json.dumps(
            {
                "day": None,
                "equity_start": None,
                "loss_halted": False,
                "execution": {"reason": "order_outcome_unknown", "originals": originals},
                "order_attempts": [],
            }
        )
    )
    async with session(tmp_path, selected) as (_, paper, task, stop, _):
        await until(task, lambda: len(paper.open_before_place) == 1)
        stop.set()
        await asyncio.wait_for(task, 2)
    assert events(tmp_path)[-1]["outcome"] == "stopped"
    assert json.loads(path.read_text())["execution"] is None
