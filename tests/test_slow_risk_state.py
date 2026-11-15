"""Real paper runner with slow state storage and a continuously fresh offline feed."""

import asyncio
import importlib
import importlib.util
import os
import socket
import time
from decimal import Decimal as D

import httpx
import pytest

from glyde_mm.client import OneApiClient
from glyde_mm.config import Config, MarketConfig, RiskConfig
from glyde_mm.paper import PaperExchange
from glyde_mm.runner import run
from glyde_mm.testing import FakeOneApiServer


async def test_slow_fsync_does_not_create_staleness_churn_or_exhaust_twelve_order_cap(
    tmp_path, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("slow-state regression must stay offline")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    writer_module = importlib.import_module(
        "glyde_mm.risk_state"
        if importlib.util.find_spec("glyde_mm.risk_state")
        else "glyde_mm.risk"
    )
    syncs = []

    class SlowStorage:
        def __getattr__(self, name):
            return getattr(os, name)

        def fsync(self, descriptor):
            delay = 0.2 if len(syncs) % 2 == 0 else 1.1
            syncs.append(delay)
            time.sleep(delay)
            os.fsync(descriptor)

    monkeypatch.setattr(writer_module, "os", SlowStorage())
    config = Config(
        mode="paper",
        paper_balance=D(1000),
        strategy_config={
            "bid_spread": "0.01",
            "ask_spread": "0.01",
            "order_refresh_time": 30,
            "max_order_age": 60,
            "refresh_tolerance_pct": "0.001",
        },
        markets=(
            MarketConfig("BTC", D("0.0005"), {"order_amount": "0.0001"}),
            MarketConfig("ETH", D("0.01"), {"order_amount": "0.002"}),
        ),
        risk=RiskConfig(
            max_position=D("0.0005"),
            max_order_notional=D(25),
            max_total_notional=D(75),
            max_daily_loss=D(5),
            max_orders_per_minute=12,
            stale_book_seconds=D(5),
            account_age_seconds=D(10),
        ),
    )
    wire = FakeOneApiServer(timestamp_ms=time.time_ns() // 1_000_000)
    paper, stop, reports, feed_cuts = PaperExchange(config.paper_balance), asyncio.Event(), [], []

    def publish():
        feed_cuts.append(time.monotonic())
        wire.now_ms = time.time_ns() // 1_000_000
        wire.set_book("BTC", bids=[("98998", "1")], asks=[("101002", "1")])
        wire.set_book("ETH", bids=[("2969.9", "100")], asks=[("3030.1", "100")])

    async def feed():
        while not stop.is_set():
            publish()
            await asyncio.sleep(0.05)

    publish()
    feeder = asyncio.create_task(feed())
    async with httpx.AsyncClient(transport=wire.http_transport()) as http:
        source = OneApiClient(config, http_client=http, connect_ws=wire.connect_ws)
        task = asyncio.create_task(
            run(
                config,
                state_directory=tmp_path,
                client_factory=lambda _: source,
                paper_factory=lambda _: paper,
                stop_event=stop,
                report=reports.append,
            )
        )
        try:
            async with asyncio.timeout(85):
                done, _ = await asyncio.wait({task}, timeout=45)
                if done:
                    await task
                    pytest.fail("runner exited before the observation ended")
                assert len(await paper.open_orders()) == 4
                assert not any("quoting paused" in line for line in reports)
                assert max(b - a for a, b in zip(feed_cuts, feed_cuts[1:], strict=False)) < 5
                stop.set()
                await task
        finally:
            stop.set()
            if not task.done():
                await asyncio.wait_for(task, 20)
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
    assert set(syncs) == {0.2, 1.1}
    assert not await paper.open_orders()
    assert not any(row["path"].endswith("/exchange") for row in wire.requests)
