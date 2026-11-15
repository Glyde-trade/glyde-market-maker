from __future__ import annotations

import json
import socket
from dataclasses import replace
from decimal import Decimal as D

import pytest

from glyde_mm.book_history import read_window, record_books
from glyde_mm.config import Config, RiskConfig
from glyde_mm.replay import replay

BASE = 1700000000000


def wire(i, bid="99", ask="101"):
    return {
        "coin": "BTC",
        "time": BASE + i * 1000,
        "levels": [[{"px": bid, "sz": "2"}], [{"px": ask, "sz": "2"}]],
    }


def recording(path, *, gap=False, old_source=False):
    rows = [{"format": "glyde.public-book", "version": 1, "symbol": "BTC", "size_decimals": 2}]
    for i in range(61):
        if gap and 1 <= i < 10:
            continue
        data = wire(i, *({1: ("96", "98"), 2: ("102", "104")}.get(i, ("99", "101"))))
        if old_source:
            data["time"] -= 60000
        rows.append({"received_at_ms": BASE + i * 1000, "book": data})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def config():
    return Config(
        mode="live",
        strategy_config={"order_amount": "0.01", "bid_spread": "0.02", "ask_spread": "0.02"},
        risk=RiskConfig(max_position=D("0.1"), max_order_notional=D(25)),
    )


async def test_replay_real_strategy_fill_cash_oracle_without_network_or_live_keys(
    tmp_path, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline replay must never connect")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.delenv("GLYDE_API_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GLYDE_READ_API_KEY", raising=False)
    path = recording(tmp_path / "books.jsonl")
    result = await replay(config(), path, 1)
    assert result.fills == 2
    assert result.inventory == 0 and result.pnl == D("0.04")
    assert result.risk_stop is None
    assert result.end_ms - result.start_ms == 60000


@pytest.mark.parametrize(
    "gap,old,reason", [(True, False, "stale_book"), (False, True, "stale_book_source")]
)
async def test_recorded_gap_or_old_source_stops_instead_of_inventing_freshness(
    tmp_path, gap, old, reason
):
    result = await replay(config(), recording(tmp_path / "books.jsonl", gap=gap, old_source=old), 1)
    assert result.risk_stop == reason
    assert result.end_ms < BASE + 60000
    assert result.fills == 0


async def test_replay_daily_loss_uses_paper_marked_equity(tmp_path):
    selected = config()
    selected = replace(selected, risk=replace(selected.risk, max_daily_loss=D("0.005")))
    result = await replay(selected, recording(tmp_path / "books.jsonl"), 1)
    assert result.risk_stop == "daily_loss"
    assert result.fills == 1 and result.pnl == D("-0.01")


@pytest.mark.parametrize(
    "strategy,values",
    [
        ("grid", {"lower_price": "90", "upper_price": "110", "levels": 3, "order_amount": "0.01"}),
        ("avellaneda_market_making", {"order_amount": "0.01", "volatility_samples": 2}),
    ],
)
async def test_shared_factory_supports_each_other_strategy(tmp_path, strategy, values):
    result = await replay(
        replace(config(), strategy=strategy, strategy_config=values),
        recording(tmp_path / "books.jsonl"),
        1,
    )
    assert result.pnl.is_finite()


def test_window_selects_exact_last_minute_and_refuses_missing_or_wrong_data(tmp_path):
    path = recording(tmp_path / "books.jsonl")
    size, start, rows = read_window(path, "BTC", 1)
    assert size == 2 and start == BASE and len(rows) == 61
    with pytest.raises(ValueError, match="cover"):
        read_window(path, "BTC", 2)
    with pytest.raises(ValueError, match="incompatible"):
        read_window(path, "ETH", 1)
    lines = path.read_text().splitlines()
    broken = json.loads(lines[-1])
    broken["received_at_ms"] = BASE - 1
    lines[-1] = json.dumps(broken)
    path.write_text("\n".join(lines))
    with pytest.raises(ValueError, match="regressed"):
        read_window(path, "BTC", 1)


async def test_recorder_is_public_only_exclusive_and_preserves_source_times(tmp_path, monkeypatch):
    import glyde_mm.book_history as history

    now = BASE

    class Source:
        def __init__(self, selected):
            assert selected.mode == "paper"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def meta(self):
            return {"universe": [{"name": "BTC", "szDecimals": 2}]}

        async def stream(self, subscriptions):
            nonlocal now
            assert subscriptions == [{"type": "l2Book", "coin": "BTC"}]
            for i in range(61):
                now = BASE + i * 1000
                yield {"channel": "l2Book", "data": wire(i), "private": "DO-NOT-RETAIN"}

    monkeypatch.setattr(history.time, "time_ns", lambda: now * 1000000)
    path = tmp_path / "books.jsonl"
    assert await record_books(config(), path, 1, client_factory=Source) == 61
    assert "DO-NOT-RETAIN" not in path.read_text()
    assert read_window(path, "BTC", 1)[2][-1].book.timestamp_ms == BASE + 60000
    with pytest.raises(FileExistsError):
        await record_books(config(), path, 1, client_factory=Source)


def test_replay_cli_never_uses_live_runner(tmp_path, monkeypatch, capsys):
    from glyde_mm import cli

    path = recording(tmp_path / "books.jsonl")
    cfg = tmp_path / "bot.yaml"
    cfg.write_text('mode: live\nstrategy_config: {order_amount: "0.01"}\n')
    monkeypatch.setattr(cli, "run_with_signals", lambda *_: pytest.fail("live runner called"))
    assert (
        cli.main(["replay", "--config", str(cfg), "--book-file", str(path), "--minutes", "1"]) == 0
    )
    assert "PAPER REPLAY" in capsys.readouterr().out


async def test_book_only_replay_refuses_ping_pong_without_identified_fills(tmp_path):
    selected = replace(config(), strategy_config={"ping_pong_enabled": True})
    with pytest.raises(ValueError, match="identified tape fills"):
        await replay(selected, tmp_path / "not-read.jsonl", 1)
