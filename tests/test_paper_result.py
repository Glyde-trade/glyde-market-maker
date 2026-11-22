"""A bounded first paper run produces an exact, private result without keys."""

import argparse
import asyncio
import json
import signal
import stat
import time
from decimal import Decimal as D

import httpx
import pytest
import yaml

from glyde_mm import cli, runner
from glyde_mm.config import Config, load_config
from glyde_mm.metrics import Metrics
from glyde_mm.models import Book, Level, OrderRequest
from glyde_mm.paper import PaperExchange
from glyde_mm.paper_result import PaperResult
from glyde_mm.testing import FakeOneApiServer


def current_book(mid, stamp=None):
    return Book(
        "BTC",
        (Level(D(mid) - 1, D(10)),),
        (Level(D(mid) + 1, D(10)),),
        time.time_ns() // 1_000_000 if stamp is None else stamp,
    )


@pytest.mark.parametrize(
    "text,seconds", [("20", 20), ("20s", 20), ("10m", 600), ("1h", 3600), ("0.1s", 0.1)]
)
def test_duration_units(text, seconds):
    assert cli.duration(text) == seconds


@pytest.mark.parametrize("text", ["0", "-1", "inf", "nan", "25h", "1day", "", "1e5"])
def test_invalid_or_unbounded_duration_refuses(text):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.duration(text)


async def test_pnl_drawdown_inventory_and_private_exclusive_output(tmp_path):
    now = [0.0]
    config = Config(paper_balance=D(1000))
    result = PaperResult(tmp_path / "result.json", config, clock=lambda: now[0])
    metrics = Metrics(config, collect=True)
    with metrics:
        assert metrics.server is None
        paper = PaperExchange(D(1000))
        await paper.on_book(current_book(100, 1000))
        await result.observe(paper, metrics)
        await paper.place_orders([OrderRequest("BTC", "buy", D(1), D(101), time_in_force="Gtc")])
        now[0] = 1
        await result.observe(paper, metrics)
        await paper.on_book(current_book(99, 2000))
        now[0] = 2
        await result.observe(paper, metrics)
        await paper.on_book(current_book(103, 3000))
        await paper.place_orders([OrderRequest("BTC", "sell", D(1), D(102), time_in_force="Gtc")])
        now[0] = 3
        await result.observe(paper, metrics)
        result.outcome = "stopped"
        result.cleanup_confirmed = True
        row = result.write("duration_elapsed")
    assert row["uptime_seconds"] == 3
    assert row["realized_pnl_usdc"] == "1"
    assert row["unrealized_pnl_usdc"] == "0"
    assert row["max_drawdown_usdc"] == "2"
    assert row["fills"] == 2
    assert [point["inventory"]["BTC"] for point in row["inventory_path"]] == ["0", "1", "0"]
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    before = result.path.read_bytes()
    with pytest.raises(FileExistsError):
        PaperResult(result.path, config)
    assert result.path.read_bytes() == before
    link = tmp_path / "link.json"
    link.symlink_to(result.path)
    with pytest.raises(OSError):
        PaperResult(link, config)
    assert result.path.read_bytes() == before


async def test_inventory_path_is_bounded_while_drawdown_keeps_observing(tmp_path):
    result = PaperResult(tmp_path / "result.json", Config())
    paper = PaperExchange()
    await paper.on_book(current_book(100))
    metrics = Metrics(Config(), collect=True)
    for i in range(10002):
        paper._positions["BTC"] = D(i % 2)
        paper._entries["BTC"] = D(100)
        await result.observe(paper, metrics)
    paper._cash -= D(5)
    await result.observe(paper, metrics)
    row = result.write("completed")
    assert len(row["inventory_path"]) == 10000
    assert row["inventory_path_truncated"] is True
    assert row["max_drawdown_usdc"] == "5"


@pytest.mark.parametrize("failure", [None, "stale", "gap", "disconnect", "refusal"])
def test_actual_cli_timer_public_tape_report_and_fail_closed_paths(monkeypatch, tmp_path, failure):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "mode": "live",
                "paper_balance": "1000",
                "strategy_config": {
                    "bid_spread": "0.003",
                    "ask_spread": "0.003",
                    "order_amount": "0.001",
                },
                "risk": {
                    "max_position": "0.01",
                    "max_order_notional": "25",
                    "stale_book_seconds": "5",
                },
            }
        )
    )
    output = tmp_path / "summary.json"
    fake = FakeOneApiServer(timestamp_ms=time.time_ns() // 1_000_000)
    fake.set_book("BTC", bids=[("99", "10")], asks=[("101", "10")])
    monkeypatch.setattr(Config, "secret", lambda *_: pytest.fail("paper read a credential"))
    received = []
    real_run = runner.run
    if failure == "stale":
        original_guard = runner.RiskGuard
        offset = [0.0]
        monkeypatch.setattr(
            runner,
            "RiskGuard",
            lambda *a, **kw: original_guard(
                *a,
                clock=lambda: time.monotonic() + offset[0],
                wall_clock=lambda: time.time() + offset[0],
                **kw,
            ),
        )

    async def run(selected, **kwargs):
        assert selected.mode == "paper"
        assert kwargs["paper_result"] is not None
        paper = PaperExchange(D(1000), clock_ms=lambda: fake.now_ms)
        placed = asyncio.Event()
        original = paper.place_orders

        async def place(rows):
            result = await original(rows)
            placed.set()
            return result

        paper.place_orders = place
        from glyde_mm.client import OneApiClient

        async with httpx.AsyncClient(transport=fake.http_transport()) as http:
            source = OneApiClient(selected, http_client=http, connect_ws=fake.connect_ws)

            async def feed():
                await placed.wait()
                if failure == "gap":
                    for ws in tuple(fake._connections):
                        ws.push({"channel": "glydeSafety", "data": {"sourceUnsafe": True}})
                    return
                if failure == "disconnect":
                    await fake.disconnect()
                    return
                if failure == "stale":
                    offset[0] = 6
                    return
                fake.advance_time(1)
                fake.market_order("BTC", buy=False, size="0.001")
                for _ in range(20):
                    await asyncio.sleep(0)
                fake.advance_time(1)
                fake.market_order("BTC", buy=True, size="0.001")

            feeder = asyncio.create_task(feed())
            try:
                await real_run(
                    selected,
                    client_factory=lambda _: source,
                    paper_factory=lambda _: paper,
                    report=lambda _: None,
                    **kwargs,
                )
            finally:
                feeder.cancel()
                await asyncio.gather(feeder, return_exceptions=True)
                received.extend(fake.requests)

    monkeypatch.setattr(cli, "run_strategy", run)
    if failure == "refusal":
        data = yaml.safe_load(path.read_text())
        data["risk"]["max_order_notional"] = "0.01"
        path.write_text(yaml.safe_dump(data))
    code = cli.main(
        [
            "paper",
            str(path),
            "--duration",
            "0.1s" if failure is None else "0.5s",
            "--report",
            str(output),
        ]
    )
    row = json.loads(output.read_text())
    assert row["cleanup_confirmed"] and row["open_quotes_at_end"] == 0
    assert all(request["body"].get("type") == "meta" for request in received)
    assert not fake._connections
    if failure is None:
        assert code == 0 and row["termination"] == "duration_elapsed"
        assert row["outcome"] == "stopped"
        assert row["quotes"]["placed"] == 2 and row["fills"] == 2
        assert D(row["realized_pnl_usdc"]) == D("0.0006")
        assert D(row["unrealized_pnl_usdc"]) == 0
    elif failure == "stale":
        # A stale book pauses quoting; the timer, not a risk stop, ends the session.
        assert code == 0 and row["pause_counts"]["stale"] == 1
        assert row["quotes"]["placed"] == 2
        assert row["outcome"] == "stopped"
    elif failure == "gap":
        assert code == 1 and row["pause_counts"]["gap"] == 1
        assert row["outcome"] == "transport_error"
    elif failure == "disconnect":
        # The timer stops the run while it waits to reconnect; a drop is not a failure.
        assert code == 0 and row["pause_counts"]["disconnect"] == 1
        assert row["outcome"] == "stopped"
    else:
        assert code == 1 and row["quotes"]["refused_by_reason"] == {"max_order_notional": 2}


async def test_signal_is_not_duration_completion_and_summary_is_retained(monkeypatch, tmp_path):
    handlers = {}
    monkeypatch.setattr(
        signal, "signal", lambda kind, callback: handlers.setdefault(kind, callback)
    )

    async def selected_run(selected, *, stop_event, paper_result, **kwargs):
        handlers[signal.SIGINT](signal.SIGINT, None)
        await stop_event.wait()
        paper_result.outcome = "stopped"
        paper_result.cleanup_confirmed = True

    monkeypatch.setattr(cli, "run_strategy", selected_run)
    assert (
        await cli.run_with_signals(
            Config(), tmp_path, duration_seconds=5, report_path=tmp_path / "result.json"
        )
        is False
    )
    assert json.loads((tmp_path / "result.json").read_text())["termination"] == "signal"


@pytest.mark.parametrize("symbol", ["btc", "eth"])
def test_conservative_examples_are_paper_only(symbol):
    from pathlib import Path

    config = load_config(Path(__file__).parents[1] / "examples" / f"paper-{symbol}.yaml")
    assert config.mode == "paper" and config.symbol == symbol.upper()
    assert config.risk.max_daily_loss <= 5 and config.risk.max_order_notional <= 25


def test_existing_report_refuses_before_runner_and_is_never_overwritten(monkeypatch, tmp_path):
    config = tmp_path / "paper.yaml"
    config.write_text("mode: paper\n")
    output = tmp_path / "result.json"
    output.write_text("existing result")
    monkeypatch.setattr(cli, "run_strategy", lambda *_args, **_kwargs: pytest.fail("runner opened"))
    assert cli.main(["paper", str(config), "--duration", "1s", "--report", str(output)]) == 1
    assert output.read_text() == "existing result"


def test_report_write_failure_is_nonzero_and_does_not_print_exception(
    monkeypatch, tmp_path, capsys
):
    config = tmp_path / "paper.yaml"
    config.write_text("mode: paper\n")

    class BrokenResult(PaperResult):
        def write(self, termination):
            self._file.close()
            raise OSError("private-error-data-not-for-output")

    async def selected_run(*_args, **_kwargs):
        return

    monkeypatch.setattr(cli, "PaperResult", BrokenResult)
    monkeypatch.setattr(cli, "run_strategy", selected_run)
    assert (
        cli.main(
            ["paper", str(config), "--duration", "1s", "--report", str(tmp_path / "result.json")]
        )
        == 1
    )
    assert "private-error-data" not in capsys.readouterr().err
