"""Synthetic execution/WS captures; no keys, private accounts or historical-profit claim."""

import json
import socket
from decimal import Decimal as D
from pathlib import Path

import pytest

from glyde_mm.cli import main
from glyde_mm.config import Config
from glyde_mm.reconciliation import reconcile_session

FIXTURE = Path(__file__).parent / "fixtures/reconciliation/session.jsonl"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("reconciliation cannot connect or load credentials")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(Config, "secret", denied)


def edited(tmp_path, change):
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    change(rows)
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


async def test_exact_order_attribution_partial_missed_slippage_and_fee_rebate():
    result = await reconcile_session(FIXTURE)
    assert D(result["matched_slippage_usdc"]) == D("-.15")
    assert D(result["fee_impact_usdc"]) == D(".005")
    assert D(result["missed_live_quantity"]) == D(".25")
    assert D(result["live_only_quantity"]) == D(".25")
    buy, sell = result["orders"]
    assert D(buy["live_quantity"]) == D(".25") and D(buy["paper_quantity"]) == D(".5")
    assert D(sell["live_quantity"]) == D(".75") and D(sell["paper_quantity"]) == D(".5")
    assert D(buy["adverse_slippage_per_base"]) == D("-.2")
    assert D(sell["adverse_slippage_per_base"]) == D("-.2")
    assert D(sell["live_fee_usdc"]) < 0  # Maker rebates are not clipped to zero.


async def test_unknown_fee_currency_is_unavailable_not_free(tmp_path):
    path = edited(
        tmp_path,
        lambda rows: [
            fill.update(feeToken="HYPE")
            for row in rows
            if row.get("type") == "private"
            for fill in row["frame"]["data"]["fills"]
        ],
    )
    result = await reconcile_session(path)
    assert result["fee_impact_usdc"] is None
    assert all(row["live_fee_usdc"] is None for row in result["orders"])


async def test_empty_complete_private_capture_is_not_missing_capture(tmp_path):
    path = edited(
        tmp_path,
        lambda rows: [
            row["frame"]["data"].update(fills=[]) for row in rows if row.get("type") == "private"
        ],
    )
    result = await reconcile_session(path)
    assert D(result["missed_live_quantity"]) == 1
    assert D(result["fee_impact_usdc"]) == 0
    assert result["matched_slippage_usdc"] is None and D(result["matched_quantity"]) == 0
    assert all(
        row["live_vwap"] is None and row["matched_slippage_usdc"] is None
        for row in result["orders"]
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda rows: rows.pop(),
        lambda rows: rows.__setitem__(
            slice(None),
            [
                row
                for row in rows
                if not (row.get("type") == "public" and row["frame"]["channel"] == "trades")
            ],
        ),
        lambda rows: rows[-1].update(private_complete=False),
        lambda rows: rows[-1].update(public_complete=False),
        lambda rows: rows.__setitem__(
            slice(None), [row for row in rows if row.get("type") != "private"]
        ),
        lambda rows: rows.__setitem__(
            slice(None), [row for row in rows if row.get("type") != "order"]
        ),
        lambda rows: rows[2]["receipt"].update(status="unknown"),
        lambda rows: rows[9]["receipt"].update(status="unknown"),
        lambda rows: rows[5]["frame"]["data"].update(user="0x" + "22" * 20),
        lambda rows: rows[5]["frame"]["data"]["fills"][0].update(cloid="0x" + "33" * 16),
        lambda rows: rows[8]["frame"]["data"]["fills"][0].update(sz=".5"),
        lambda rows: rows[5]["frame"]["data"]["fills"][0].update(sz="2"),
        lambda rows: rows[4].update(received_at_ms=999),
        lambda rows: rows[4]["frame"]["data"][0].update(sz="NaN"),
        lambda rows: rows[2]["receipt"].update(client_order_id="0x" + "77" * 16),
        lambda rows: rows[5]["frame"]["data"]["fills"][0].update(px="99.2"),
        lambda rows: rows[7]["frame"]["data"]["fills"][0].update(px="100.8"),
    ],
)
async def test_incomplete_ambiguous_mismatched_or_conflicting_capture_refuses(tmp_path, change):
    with pytest.raises((ValueError, RuntimeError)):
        await reconcile_session(edited(tmp_path, change))


async def test_other_actor_orders_never_change_session_statistics(tmp_path):
    def change(rows):
        for row in rows:
            if row.get("type") == "private":
                row["frame"]["data"]["fills"].append(
                    {"oid": 9999, "tid": 555, "px": "999999", "sz": "99999"}
                )

    assert await reconcile_session(edited(tmp_path, change)) == await reconcile_session(FIXTURE)


def test_actual_cli_runs_entirely_offline_without_state_write(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    assert main(["reconcile", "--session-file", str(FIXTURE)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert D(result["matched_slippage_usdc"]) == D("-.15")
    assert set(tmp_path.iterdir()) == before
    assert "user" not in result and "paper_fee_basis" in result


async def test_numeric_json_economics_keep_decimal_precision(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(FIXTURE.read_text().replace('"px":"98.8"', '"px":98.800000000000000001'))
    result = await reconcile_session(path)
    assert D(result["orders"][0]["live_vwap"]) == D("98.800000000000000001")


async def test_extreme_public_economics_are_refused_before_arithmetic(tmp_path):
    path = edited(
        tmp_path, lambda rows: rows[1]["frame"]["data"]["levels"][0][0].update(px="1e-999999999")
    )
    with pytest.raises(ValueError, match="capture bounds"):
        await reconcile_session(path)
