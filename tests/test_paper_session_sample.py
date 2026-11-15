from __future__ import annotations

import json
import socket
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from glyde_mm.cli import main
from glyde_mm.config import Config, load_config
from glyde_mm.replay import replay
from glyde_mm.report import summarize

ROOT = Path(__file__).resolve().parents[1]
RECORDING = ROOT / "examples/recordings/paper-session.jsonl"
CONFIG = ROOT / "examples/paper-session.yaml"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("sample must never connect or read credentials")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(Config, "secret", forbidden)


def test_recorded_fixture_cli_replay_to_report_and_no_risk_state(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG.read_text())
    assert (
        main(["replay", "--config", str(config), "--book-file", str(RECORDING), "--minutes", "1"])
        == 0
    )
    terminal = capsys.readouterr().out
    assert "inventory=0.00 | simulated_pnl=+0.04 USDC | fills=2 | risk_stop=none" in terminal
    assert "simulated_seconds=60" in terminal
    state = tmp_path / ".glyde-mm"
    assert list(state.iterdir()) == [state / "logs"]
    assert main(["report", str(state / "logs")]) == 0
    row = json.loads(capsys.readouterr().out)["runs"][0]
    assert row["mode"] == "paper" and row["outcome"] == "completed"
    assert row["last_progress"] == {"book_messages": 61, "pending_orders": 0, "paper_fills": 2}
    assert row["history_truncated"] is False
    assert row["version"] and row["commit"]


async def test_replay_risk_stop_is_not_reported_as_success(tmp_path):
    config = tmp_path / "config.yaml"
    values = yaml.safe_load(CONFIG.read_text())
    values["risk"]["max_daily_loss"] = "0.005"
    config.write_text(yaml.safe_dump(values))
    result = await replay(load_config(config), RECORDING, 1, state_directory=tmp_path / "state")
    assert result.risk_stop == "daily_loss" and result.pnl == Decimal("-0.01")
    row = summarize(tmp_path / "state/logs")["runs"][0]
    assert row["outcome"] == "risk_stop"
    assert row["last_progress"]["pending_orders"] == 0


async def test_invalid_recording_has_error_report_without_started_strategy(tmp_path):
    recording = tmp_path / "bad.jsonl"
    recording.write_text("{}\n")
    with pytest.raises(ValueError):
        await replay(load_config(CONFIG), recording, 1, state_directory=tmp_path / "state")
    row = summarize(tmp_path / "state/logs")["runs"][0]
    assert row["outcome"] == "error" and row["last_progress"] is None
