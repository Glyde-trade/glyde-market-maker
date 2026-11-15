from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from decimal import Decimal as D

import pytest

from glyde_mm.config import Config, RiskConfig
from glyde_mm.models import Account, CancelResult, OrderResult
from glyde_mm.paper import PaperExchange
from glyde_mm.risk import RiskViolation
from glyde_mm.runner import risk_state_path, run


class Source:
    def __init__(self, config, *, stop, placed):
        self.config, self.stop, self.placed = config, stop, placed
        self.disconnected = False
        self.wire_time = time.time_ns() // 1_000_000
        self.live_orders = []
        self.cancellations = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def meta(self):
        return {"universe": [{"name": "BTC", "szDecimals": 2}]}

    async def stream(self, subscriptions):
        assert subscriptions == [{"type": "l2Book", "coin": "BTC"}]
        yield {"channel": "subscriptionResponse", "data": {}}
        yield {
            "channel": "l2Book",
            "data": {
                "coin": "BTC",
                "time": self.wire_time,
                "levels": [[{"px": "99", "sz": "2"}], [{"px": "101", "sz": "2"}]],
            },
        }
        await self.placed.wait()
        if self.disconnected:
            raise ConnectionError("synthetic disconnect")
        await self.stop.wait()
        await asyncio.Event().wait()

    async def account(self):
        assert self.config.mode == "live", "paper must not read a private account"
        return Account(D("1000"))

    async def open_orders(self):
        assert self.config.mode == "live"
        return []

    async def place_orders(self, orders):
        assert self.config.mode == "live", "paper must never reach the signing transport"
        self.live_orders.extend(orders)
        self.placed.set()
        self.stop.set()
        return [OrderResult("resting", str(i), row.client_order_id) for i, row in enumerate(orders)]

    async def cancel_orders(self, orders):
        return [CancelResult("cancelled", row.order_id, row.client_order_id) for row in orders]

    async def cancel_all(self, symbol=None):
        self.cancellations += 1


class Paper(PaperExchange):
    def __init__(self, balance, *, placed, stop, stop_on_place=True):
        super().__init__(balance)
        self.placed, self.stop, self.stop_on_place = placed, stop, stop_on_place
        self.submissions = []
        self.cancellations = 0

    async def place_orders(self, orders):
        self.submissions.extend(orders)
        result = await super().place_orders(orders)
        self.placed.set()
        if self.stop_on_place:
            self.stop.set()
        return result

    async def cancel_all(self, symbol=None):
        self.cancellations += 1
        await super().cancel_all(symbol)


def config(mode="paper", stale="5"):
    return Config(
        mode=mode,
        symbol="BTC",
        owner="0x" + "11" * 20,
        vault_address="0x" + "22" * 20,
        strategy_config={"order_amount": "0.01", "bid_spread": "0.02", "ask_spread": "0.02"},
        risk=RiskConfig(
            max_position=D("0.1"), max_order_notional=D("25"), stale_book_seconds=D(stale)
        ),
    )


async def scenario(
    tmp_path, *, mode="paper", disconnected=False, stale="5", old_book=False, report=lambda _: None
):
    stop, placed = asyncio.Event(), asyncio.Event()
    selected = config(mode, stale)
    source = Source(selected, stop=stop, placed=placed)
    source.disconnected = disconnected
    if old_book:
        source.wire_time -= 60000
    paper = Paper(
        selected.paper_balance,
        placed=placed,
        stop=stop,
        stop_on_place=not disconnected and stale == "5",
    )
    task = run(
        selected,
        state_directory=tmp_path,
        client_factory=lambda _: source,
        paper_factory=lambda _: paper,
        stop_event=stop,
        report=report,
    )
    return task, source, paper


async def test_paper_places_real_strategy_quotes_through_risk_without_private_reads(tmp_path):
    task, source, paper = await scenario(tmp_path)
    await asyncio.wait_for(task, 2)
    assert len(paper.submissions) == 2
    assert all(order.client_order_id.startswith("0x") for order in paper.submissions)
    assert source.live_orders == []
    assert await paper.open_orders() == []
    assert paper.cancellations >= 1
    assert list(tmp_path.glob("paper-*.json"))
    events = [
        json.loads(line)
        for line in next(tmp_path.glob("logs/run-*/events.jsonl")).read_text().splitlines()
    ]
    assert events[0]["event"] == "run_header"
    assert {"version", "commit"} <= events[0].keys()
    assert events[-1]["event"] == "run_ended" and events[-1]["outcome"] == "stopped"
    assert "strategy_started" in {row["event"] for row in events}


async def test_explicit_live_routes_only_through_managed_transport_and_cancels_on_stop(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "synthetic-not-a-signing-key")
    monkeypatch.setenv("GLYDE_READ_API_KEY", "synthetic-read")
    task, source, paper = await scenario(tmp_path, mode="live")
    await asyncio.wait_for(task, 2)
    assert len(source.live_orders) == 2 and paper.submissions == []
    assert source.cancellations >= 1


async def test_source_disconnect_cancels_quotes_without_reconnect_or_resend(tmp_path):
    task, source, paper = await scenario(tmp_path, disconnected=True)
    with pytest.raises(ConnectionError, match="synthetic"):
        await asyncio.wait_for(task, 2)
    assert len(paper.submissions) == 2 and source.live_orders == []
    assert await paper.open_orders() == []


async def test_silent_stale_stream_triggers_watchdog_cancel(tmp_path):
    task, _, paper = await scenario(tmp_path, stale="0.1")
    with pytest.raises(RiskViolation, match="stale"):
        await asyncio.wait_for(task, 2)
    assert len(paper.submissions) == 2
    assert await paper.open_orders() == []


async def test_stale_initial_source_cut_never_dispatches(tmp_path):
    task, source, paper = await scenario(tmp_path, old_book=True)
    with pytest.raises(RiskViolation, match="stale_book_source"):
        await asyncio.wait_for(task, 2)
    assert paper.submissions == source.live_orders == []


def test_loss_state_isolated_by_mode_and_registered_authority(tmp_path):
    assert risk_state_path(config("paper"), tmp_path) != risk_state_path(config("live"), tmp_path)


def test_cli_paper_forces_simulation_and_run_uses_explicit_config(tmp_path, monkeypatch):
    from glyde_mm import cli

    seen = []

    async def fake_run(config, **kwargs):
        seen.append((config.mode, kwargs["state_directory"]))

    monkeypatch.setattr(cli, "run_strategy", fake_run)
    monkeypatch.setattr("builtins.input", lambda _: "BTC")
    path = tmp_path / "bot.yaml"
    path.write_text("mode: live\n")
    assert cli.main(["paper", str(path)]) == 0
    assert cli.main(["run", str(path)]) == 0
    assert seen == [("paper", tmp_path / ".glyde-mm"), ("live", tmp_path / ".glyde-mm")]


async def test_grid_config_is_selectable_by_the_same_safe_runner(tmp_path):
    stop, placed = asyncio.Event(), asyncio.Event()
    selected = replace(
        config(),
        strategy="grid",
        strategy_config={
            "lower_price": "90",
            "upper_price": "110",
            "order_amount": "0.01",
            "levels": 5,
        },
    )
    source = Source(selected, stop=stop, placed=placed)
    paper = Paper(selected.paper_balance, stop=stop, placed=placed)
    await asyncio.wait_for(
        run(
            selected,
            state_directory=tmp_path,
            client_factory=lambda _: source,
            paper_factory=lambda _: paper,
            stop_event=stop,
            report=lambda _: None,
        ),
        2,
    )
    assert len(paper.submissions) == 4
    assert all(D("90") <= order.price <= D("110") for order in paper.submissions)
    assert await paper.open_orders() == []


async def test_cli_installs_signal_driven_shutdown_without_sending_real_signals(
    tmp_path, monkeypatch
):
    import signal

    from glyde_mm import cli

    installed, restored = {}, []

    def set_handler(kind, callback):
        if callable(callback):
            installed[kind] = callback
        else:
            restored.append(kind)
        return None

    monkeypatch.setattr(signal, "signal", set_handler)

    async def fake_run(config, *, state_directory, stop_event, allow_existing_exposure=False):
        assert allow_existing_exposure is False
        installed[signal.SIGTERM](signal.SIGTERM, None)
        await asyncio.wait_for(stop_event.wait(), 1)

    monkeypatch.setattr(cli, "run_strategy", fake_run)
    await cli.run_with_signals(config(), tmp_path)
    assert set(restored) == {signal.SIGTERM, signal.SIGINT}


@pytest.mark.parametrize("stage", ["preset", "initial_book", "account"])
async def test_stop_fences_bootstrap_before_any_quote_but_preserves_cleanup(tmp_path, stage):
    stop, placed = asyncio.Event(), asyncio.Event()
    if stage == "preset":
        stop.set()
    selected = config()

    class StopSource(Source):
        async def stream(self, subscriptions):
            async for frame in super().stream(subscriptions):
                if stage == "initial_book" and frame.get("channel") == "l2Book":
                    stop.set()
                yield frame

    class StopPaper(Paper):
        async def account(self):
            result = await super().account()
            if stage == "account":
                stop.set()
            return result

    source = StopSource(selected, stop=stop, placed=placed)
    paper = StopPaper(selected.paper_balance, stop=stop, placed=placed)
    await asyncio.wait_for(
        run(
            selected,
            state_directory=tmp_path,
            client_factory=lambda _: source,
            paper_factory=lambda _: paper,
            stop_event=stop,
            report=lambda _: None,
        ),
        2,
    )
    assert paper.submissions == source.live_orders == []
    assert paper.cancellations == 1


async def test_paper_status_shows_active_quotes_and_final_cleanup(tmp_path):
    messages = []
    task, source, _ = await scenario(tmp_path, report=messages.append)
    await asyncio.wait_for(task, 2)
    status = next(row for row in messages if row.startswith("PAPER BTC |"))
    assert "inventory=0" in status and "simulated_pnl=+0 USDC" in status
    assert "fills=0 | open_quotes=2 | risk_stop=none" in status
    assert messages[-1].startswith("PAPER SUMMARY BTC |")
    assert "open_quotes=0 | risk_stop=none" in messages[-1]
    assert source.live_orders == []


async def test_paper_risk_stop_is_visible_after_quotes_are_cancelled(tmp_path):
    messages = []
    task, _, _ = await scenario(tmp_path, stale="0.1", report=messages.append)
    with pytest.raises(RiskViolation, match="stale"):
        await asyncio.wait_for(task, 2)
    summary = messages[-1]
    assert summary.startswith("PAPER SUMMARY BTC |")
    assert "open_quotes=0" in summary and "risk_stop=stale" in summary


async def test_paper_status_uses_simulated_equity_and_fill_ledger():
    from glyde_mm.models import Book, Level, OrderRequest
    from glyde_mm.runner import report_paper_status

    selected = config()
    paper = PaperExchange(selected.paper_balance)
    await paper.on_book(Book("BTC", (Level(D(99), D(2)),), (Level(D(101), D(2)),), 1))
    await paper.place_orders([OrderRequest("BTC", "buy", D("0.5"), D(101), time_in_force="Ioc")])
    messages = []
    await report_paper_status(paper, selected, messages.append)
    assert messages == [
        "PAPER BTC | inventory=0.5 | simulated_pnl=-0.5 USDC"
        " | fills=1 | open_quotes=0 | risk_stop=none"
    ]
    assert (await paper.account()).equity == selected.paper_balance - D("0.5")
