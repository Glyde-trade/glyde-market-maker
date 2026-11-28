from __future__ import annotations

import asyncio
import json
import socket
import time
from contextlib import asynccontextmanager
from decimal import Decimal as D

import httpx
import pytest
from eth_account import Account as Wallet

from glyde_mm import cli
from glyde_mm.client import OneApiClient, OneApiError, RateLimited
from glyde_mm.config import Config
from glyde_mm.models import (
    Account,
    CancelResult,
    HistoricalOrder,
    OpenOrder,
    OrderRequest,
    bot_client_order_id,
)
from glyde_mm.paper import PaperExchange
from glyde_mm.risk import RiskViolation
from glyde_mm.runner import risk_state_path, run
from glyde_mm.shutdown import RunnerControl, _lock, _paths, stop_bot
from glyde_mm.testing import FakeOneApiServer


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("stop tests must be offline")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)


def selected():
    return Config(mode="live", owner="0x" + "1" * 40, vault_address="0x" + "2" * 40)


class Source:
    def __init__(self):
        self.events = []
        self.rows = [OpenOrder("1", "BTC", "buy", D("0.1"), D(99), bot_client_order_id())]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def cancel_orders(self, orders):
        self.events.append(("originals", orders))
        return [CancelResult("cancelled", row.order_id, row.client_order_id) for row in orders]

    async def cancel_all(self, symbol):
        self.events.append(("cancel", symbol))
        self.rows = [row for row in self.rows if row.symbol != symbol]

    async def open_orders(self):
        self.events.append(("read",))
        return self.rows


async def test_crashed_runner_stop_cancels_only_symbol_and_preserves_positions(
    tmp_path, monkeypatch
):
    config = selected()
    monkeypatch.setenv(config.private_key_env, Wallet.create().key.hex())
    monkeypatch.setenv(config.read_api_key_env, "synthetic-read")
    fake = FakeOneApiServer(user=config.owner)
    fake.set_book("BTC", bids=[("99", "2")], asks=[("101", "2")])
    fake.set_book("ETH", bids=[("9", "2")], asks=[("11", "2")])
    writes = []

    @asynccontextmanager
    async def factory(config):
        transport = fake.http_transport()

        async def observe(request):
            if request.url.path.endswith("/exchange"):
                writes.append(json.loads(request.content)["action"]["type"])
            return await transport.handle_async_request(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(observe)) as http:
            async with OneApiClient(config, http_client=http) as client:
                yield client

    async with factory(config) as client:
        await client.place_orders(
            [
                OrderRequest("BTC", "buy", D("0.1"), D(98), bot_client_order_id()),
                OrderRequest("ETH", "buy", D("0.1"), D(8), bot_client_order_id()),
                # The account holder's own BTC order, untagged and without a cloid.
                OrderRequest("BTC", "buy", D("0.1"), D(97)),
            ]
        )
        before = await client.account()
    writes.clear()
    result, path = await stop_bot(config, tmp_path, client_factory=factory)
    assert result["outcome"] == "stopped"
    assert result["remaining_orders"] == 0
    assert result["positions_closed"] is False
    assert json.loads(path.read_text()) == result
    assert path.stat().st_mode & 0o777 == 0o600
    async with factory(config) as client:
        left = await client.open_orders()
        assert sorted((row.symbol, row.price) for row in left) == [("BTC", 97), ("ETH", 8)]
        assert (await client.account()).positions == before.positions
    assert writes == ["cancel"]
    again, _ = await stop_bot(config, tmp_path, client_factory=factory)
    assert again["outcome"] == "stopped"
    assert writes == ["cancel"]


async def test_unresolved_originals_attempted_before_discovery(tmp_path):
    config, source = selected(), Source()
    risk_state_path(config, tmp_path).write_text(
        json.dumps(
            {
                "execution": {
                    "reason": "order_outcome_unknown",
                    "originals": [
                        {"symbol": "BTC", "order_id": None, "client_order_id": "0x" + "a" * 32},
                        {"symbol": "ETH", "order_id": "2", "client_order_id": None},
                    ],
                }
            }
        )
    )
    source.rows.append(OpenOrder("7", "BTC", "buy", D("0.1"), D(98), "0x" + "a" * 32))
    result, _ = await stop_bot(config, tmp_path, client_factory=lambda _: source)
    assert result["outcome"] == "stopped"
    # Resting originals are read first, then cancelled before discovery.
    assert source.events[0] == ("read",)
    assert source.events[1][0] == "originals"
    assert source.events[1][1][0].client_order_id == "0x" + "a" * 32
    assert len(source.events[1][1]) == 1
    assert source.events[2:] == [("cancel", "BTC"), ("read",)]
    assert json.loads(risk_state_path(config, tmp_path).read_text())["execution"]


@pytest.mark.parametrize("failure", ["read", "cancel", "remaining", "malformed_state"])
async def test_unconfirmed_stop_writes_failure_not_success(tmp_path, failure):
    class Broken(Source):
        async def cancel_all(self, symbol):
            if failure == "cancel":
                raise OneApiError("secret must not enter the report")
            if failure != "remaining":
                await super().cancel_all(symbol)

        async def open_orders(self):
            if failure == "read":
                raise OneApiError("secret must not enter the report")
            return await super().open_orders()

    if failure == "malformed_state":
        risk_state_path(selected(), tmp_path).write_text("invalid")
    result, path = await stop_bot(selected(), tmp_path, client_factory=lambda _: Broken())
    assert result["outcome"] == "failed"
    assert "secret" not in path.read_text()


async def test_paper_stop_never_constructs_private_client(tmp_path):
    def forbidden(_):
        raise AssertionError("paper stop must not load a client or credentials")

    result, _ = await stop_bot(Config(), tmp_path, client_factory=forbidden)
    assert result["outcome"] == "stopped"


async def test_running_session_must_exit_before_cancellation(tmp_path):
    config = selected()
    stop = asyncio.Event()
    entered = asyncio.Event()
    source = Source()

    async def bot():
        async with RunnerControl(config, tmp_path, stop):
            entered.set()
            await stop.wait()
            await asyncio.sleep(0.01)
            source.events.append(("runner_exited",))

    task = asyncio.create_task(bot())
    await entered.wait()
    try:
        result, _ = await stop_bot(config, tmp_path, client_factory=lambda _: source)
        assert result["outcome"] == "stopped"
        assert source.events == [("runner_exited",), ("cancel", "BTC"), ("read",)]
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stuck_runner_cannot_produce_success_or_cancel_while_quoting(tmp_path):
    config, source = selected(), Source()
    async with RunnerControl(config, tmp_path, asyncio.Event()):
        result, _ = await stop_bot(
            config, tmp_path, client_factory=lambda _: source, wait_seconds=0.01
        )
    assert result["outcome"] == "failed"
    assert source.events == []


async def test_stop_operation_blocks_new_runner_and_concurrent_stop(tmp_path):
    config = selected()
    operation, _, _ = _paths(config, tmp_path)
    with _lock(operation):
        with pytest.raises(RiskViolation, match="local_session_busy"):
            async with RunnerControl(config, tmp_path, asyncio.Event()):
                pytest.fail("runner must not start while a stop owns the operation")
        result, _ = await stop_bot(config, tmp_path)
    assert result["outcome"] == "failed"


async def test_real_runner_observes_stop_request_and_scopes_its_cleanup(tmp_path):
    stop, started = asyncio.Event(), asyncio.Event()
    source = Source()

    async def meta():
        return {"universe": [{"name": "BTC", "szDecimals": 2}]}

    async def stream(_):
        yield {
            "channel": "l2Book",
            "data": {
                "coin": "BTC",
                "time": time.time_ns() // 1_000_000,
                "levels": [[{"px": "99", "sz": "2"}], [{"px": "101", "sz": "2"}]],
            },
        }
        await asyncio.Event().wait()

    async def account():
        return Account(D(1000))

    class Strategy:
        async def on_book(self, _):
            started.set()

        async def on_tick(self):
            pass

        async def stop(self):
            pass

    source.meta, source.stream, source.account = meta, stream, account

    class Paper(PaperExchange):
        async def cancel_all(self, symbol=None):
            source.events.append(("cancel", symbol))
            await super().cancel_all(symbol)

    config = Config()
    task = asyncio.create_task(
        run(
            config,
            state_directory=tmp_path,
            stop_event=stop,
            client_factory=lambda _: source,
            paper_factory=Paper,
            strategy_factory=lambda *args: Strategy(),
            report=lambda _: None,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        result, _ = await stop_bot(config, tmp_path)
        await task
        assert result["outcome"] == "stopped"
        assert stop.is_set()
        assert ("cancel", "BTC") in source.events
        assert ("cancel", None) not in source.events
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_stop_cli_no_live_confirmation_and_failure_exit(tmp_path, monkeypatch, capsys):
    path = tmp_path / "bot.yaml"
    path.write_text("mode: live\nsymbol: BTC\n")

    async def failed(config, directory):
        assert config.mode == "live" and directory == tmp_path / ".glyde-mm"
        return {"outcome": "failed"}, directory / "report.json"

    def no_prompt(_):
        pytest.fail("emergency stop must not prompt before cancelling")

    monkeypatch.setattr("builtins.input", no_prompt)
    monkeypatch.setattr("glyde_mm.shutdown.stop_bot", failed)
    assert cli.main(["stop", str(path)]) == 1
    assert "NOT confirmed" in capsys.readouterr().err


def test_stop_cli_paper_report(tmp_path, capsys):
    path = tmp_path / "bot.yaml"
    path.write_text("mode: paper\n")
    assert cli.main(["stop", str(path)]) == 0
    assert "stopped" in capsys.readouterr().out
    reports = list((tmp_path / ".glyde-mm" / "stops").glob("*.json"))
    assert len(reports) == 1


class RateLimitedVenue(Source):
    """#121: 25 persisted originals, 5 still resting, and a cancel bucket of 10 per action."""

    def __init__(self):
        super().__init__()
        self.rows = [
            OpenOrder(str(i), "BTC", "buy", D("0.1"), D(90), bot_client_order_id())
            for i in range(10930, 10935)
        ]
        self.refusals = 0

    async def historical_orders(self):
        return [HistoricalOrder(str(i), "BTC", "canceled") for i in range(10935, 10955)]

    async def cancel_orders(self, orders):
        assert len(orders) <= 10
        if self.refusals == 0:
            self.refusals += 1
            raise RateLimited("exchange refused the action: rate limited", 0.01)
        self.events.append(("originals", orders))
        ids = {row.order_id for row in orders}
        self.rows = [row for row in self.rows if row.order_id not in ids]
        return [CancelResult("cancelled", row.order_id, row.client_order_id) for row in orders]


def originals_state(config, directory, ids):
    rows = [{"symbol": "BTC", "order_id": str(i), "client_order_id": None} for i in ids]
    risk_state_path(config, directory).write_text(
        json.dumps({"execution": {"reason": "cancel_outcome_unknown", "originals": rows}})
    )


async def test_stop_after_a_rate_limited_crash_cancels_only_resting_originals(tmp_path):
    config, venue = selected(), RateLimitedVenue()
    originals_state(config, tmp_path, range(10930, 10955))
    result, _ = await stop_bot(config, tmp_path, client_factory=lambda _: venue)
    assert result["outcome"] == "stopped" and result["remaining_orders"] == 0
    cancelled = [
        row.order_id for event in venue.events if event[0] == "originals" for row in event[1]
    ]
    assert cancelled == [str(i) for i in range(10930, 10935)]  # Closed ones are skipped.


async def test_stop_report_names_the_rate_limit_class(tmp_path, monkeypatch):
    class AlwaysLimited(RateLimitedVenue):
        async def cancel_orders(self, orders):
            raise RateLimited("exchange refused the action: rate limited", 999)

        async def cancel_all(self, symbol):
            raise RateLimited("exchange refused the action: rate limited", 999)

    config = selected()
    originals_state(config, tmp_path, range(10930, 10935))
    result, path = await stop_bot(config, tmp_path, client_factory=lambda _: AlwaysLimited())
    assert result["outcome"] == "failed" and result["failure"] == "rate_limited"
    assert "0x" not in path.read_text()
