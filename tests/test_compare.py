"""All tapes are synthetic; comparisons use no network, signer or persisted risk state."""

import json
import socket
from decimal import Decimal as D

import pytest

from glyde_mm.cli import main
from glyde_mm.compare import compare_strategies, format_comparison
from glyde_mm.config import Config, RiskConfig
from glyde_mm.public_tape import read_tape, record_tape


def book(timestamp, mid="100"):
    return {
        "coin": "BTC",
        "time": timestamp,
        "levels": [[{"px": str(D(mid) - 1), "sz": "1"}], [{"px": str(D(mid) + 1), "sz": "1"}]],
    }


def event(timestamp, channel="l2Book", data=None, source="glyde"):
    return {
        "received_at_ms": timestamp,
        "source": source,
        "channel": channel,
        "data": book(timestamp) if data is None else data,
    }


def trade(timestamp, side="A", price="90", size=".02", tid=None):
    return {
        "coin": "BTC",
        "side": side,
        "px": price,
        "sz": size,
        "time": timestamp,
        "tid": timestamp if tid is None else tid,
    }


def tape(tmp_path, events):
    path = tmp_path / "tape.jsonl"
    header = {"format": "glyde.public-tape", "version": 1, "symbol": "BTC", "size_decimals": 3}
    path.write_text("\n".join(json.dumps(row) for row in [header, *events]) + "\n")
    return path


def config(**limits):
    limits.setdefault("max_orders_per_minute", 600)
    return Config(
        mode="live", risk=RiskConfig(max_position=D(1), max_order_notional=D(25), **limits)
    )


def market_events(*, hl=True):
    events = []
    for second in range(46):
        timestamp = 1000 + second * 1000
        mid = str(D(100) + D(second % 3) / 20)
        events.append(event(timestamp, data=book(timestamp, mid)))
        if hl:
            events.append(event(timestamp, source="hyperliquid"))
        if second:
            events.append(
                event(
                    timestamp + 1,
                    "trades",
                    [
                        trade(
                            timestamp + 1, "A" if second % 2 else "B", "90" if second % 2 else "110"
                        )
                    ],
                )
            )
    return events


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline comparison must not open sockets or load signers")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("glyde_mm.client.EthAccount.from_key", forbidden)


async def test_four_strategies_deterministic_real_tape_fills_and_no_state_writes(tmp_path):
    path = tape(tmp_path, market_events())
    before = set(tmp_path.iterdir())
    first = await compare_strategies(config(), path)
    second = await compare_strategies(config(), path)
    assert first == second
    assert [row.strategy for row in first.rows] == [
        "PMM",
        "inventory-skewed",
        "grid",
        "HL-reference",
    ]
    assert all(row.fills > 0 and row.status == "completed" for row in first.rows)
    assert all(row.inventory_max > row.inventory_min for row in first.rows)
    assert all(0 < row.quote_uptime_pct <= 100 for row in first.rows)
    assert first.rows[1].quote_uptime_pct < first.rows[0].quote_uptime_pct  # Real AS warmup.
    assert all(row.max_drawdown >= 0 for row in first.rows)
    assert set(tmp_path.iterdir()) == before
    assert "No live orders" in format_comparison(first)


async def test_hl_reference_absent_is_unavailable_not_glyde_fallback(tmp_path):
    result = await compare_strategies(config(), tape(tmp_path, market_events(hl=False)))
    assert result.rows[-1].fills is None
    assert result.rows[-1].pnl is None
    assert "unavailable" in result.rows[-1].status
    assert result.rows[0].fills > 0


async def test_stale_hl_reference_stops_only_that_row(tmp_path):
    events = market_events()
    events = [row for row in events if row["source"] == "glyde" or row["received_at_ms"] == 1000]
    result = await compare_strategies(config(), tape(tmp_path, events))
    assert result.rows[-1].status == "stopped: reference_stale"
    assert result.rows[-1].quote_uptime_pct < result.rows[0].quote_uptime_pct
    assert result.rows[0].status == "completed"


async def test_risk_stop_retains_inventory_marks_through_common_end_and_drawdown(tmp_path):
    events = [
        event(1000),
        event(1001, "trades", [trade(1001)]),
        event(2000, data=book(2000, "80")),
        event(3000, data=book(3000, "110")),
    ]
    result = await compare_strategies(config(max_daily_loss=D(".1")), tape(tmp_path, events))
    row = result.rows[0]
    assert row.status == "stopped: daily_loss"
    assert row.fills == 1 and row.inventory_max == D(".02")
    assert row.max_drawdown == D(".4")
    assert row.pnl == D(".204")  # Still marked through recovery, without restarting quotes.
    assert row.quote_uptime_pct == 50


async def test_old_or_duplicated_print_does_not_replay_volume(tmp_path):
    events = [
        event(1000),
        event(1001, "trades", [trade(1001)]),
        event(1002, "trades", [trade(1001)]),
        event(2000),
    ]
    result = await compare_strategies(config(), tape(tmp_path, events))
    assert result.rows[0].fills == 1
    assert result.rows[0].inventory_max == D(".02")


async def test_common_baseline_preserves_original_arrival_age(tmp_path):
    events = [
        event(1000),
        event(5000, source="hyperliquid", data=book(1000)),
        event(5001, "trades", [trade(5001)]),
        event(7000),
    ]
    result = await compare_strategies(config(), tape(tmp_path, events))
    assert result.start_ms == 5000
    assert result.rows[-1].status == "stopped: reference_stale"
    # No re-stamping of the Glyde baseline either (becomes stale at the next tick).
    assert result.rows[0].status == "stopped: stale_book"


@pytest.mark.parametrize(
    "bad",
    [
        [event(1000)],
        [event(1000), event(1001)],  # Book-only recordings cannot claim trade coverage.
        [event(1000), event(999, "trades", [])],
        [event(1000), event(2000, "trades", [trade(9000)])],
        [event(1000), event(2000, source="other")],
        [event(1000), event(2000, "trades", [{**trade(2000), "sz": "NaN"}])],
        [event(1000), event(2000, "trades", []), event(3611001)],
    ],
)
def test_malformed_missing_or_regressed_tapes_refuse_before_simulation(tmp_path, bad):
    with pytest.raises((ValueError, RuntimeError)):
        read_tape(tape(tmp_path, bad), "BTC")


def test_cli_prints_four_row_table_and_keeps_live_config_offline(tmp_path, capsys):
    path = tape(tmp_path, market_events(hl=False))
    configuration = tmp_path / "config.yaml"
    configuration.write_text("mode: live\nsymbol: BTC\n")
    assert main(["compare", "--config", str(configuration), "--tape-file", str(path)]) == 0
    output = capsys.readouterr().out
    for text in (
        "PAPER COMPARISON",
        "PMM",
        "inventory-skewed",
        "grid",
        "HL-reference",
        "drawdown",
        "uptime",
        "unavailable",
    ):
        assert text in output
    assert not (tmp_path / ".glyde-mm").exists()


async def test_public_recording_canonical_fields_exclusive_file_and_cleanup(tmp_path, monkeypatch):
    now = [1000]
    sources = []

    class Source:
        def __init__(self, selected):
            assert (
                selected.mode == "paper" and not selected.owner and selected.vault_address is None
            )
            self.closed = False
            sources.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def meta(self):
            return {"universe": [{"name": "BTC", "szDecimals": 3}]}

        async def stream(self, subscriptions):
            assert subscriptions == [{"type": c, "coin": "BTC"} for c in ("l2Book", "trades")]
            for row in [event(1000), event(1001, "trades", [trade(1001)]), event(61000)]:
                now[0] = row["received_at_ms"]
                yield {**row, "secret": "must-not-be-recorded"}

    monkeypatch.setattr("glyde_mm.public_tape.time.time_ns", lambda: now[0] * 1000000)
    path = tmp_path / "recording.jsonl"
    assert await record_tape(config(), path, 1, client_factory=Source) == 3
    assert all(source.closed for source in sources)
    assert "must-not-be-recorded" not in path.read_text()
    assert len(read_tape(path, "BTC")[1]) == 3
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        await record_tape(config(), path, 1, client_factory=Source)


async def test_default_order_rate_cap_is_not_removed_for_comparison(tmp_path):
    result = await compare_strategies(
        config(max_orders_per_minute=60), tape(tmp_path, market_events())
    )
    assert result.rows[0].status == "stopped: order_rate_limit"
    assert result.rows[0].quote_uptime_pct < 100


async def test_public_capture_option_adds_only_hl_books_and_closes_both_clients(
    tmp_path, monkeypatch
):
    import asyncio

    now = [1000]
    seen = []

    def clock():
        stamp = now[0]
        now[0] += 30000
        return stamp * 1000000

    class Public:
        def __init__(self, selected):
            self.hl = "hyperliquid" in selected.base_url
            self.closed = False
            assert (
                selected.mode == "paper" and selected.owner == "" and selected.vault_address is None
            )
            seen.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def meta(self):
            assert not self.hl
            return {"universe": [{"name": "BTC", "szDecimals": 3}]}

        async def stream(self, subscriptions):
            expected = {"l2Book"} if self.hl else {"l2Book", "trades"}
            assert {row["type"] for row in subscriptions} == expected
            for index in range(3):
                await asyncio.sleep(0)
                channel = "l2Book" if self.hl or index != 1 else "trades"
                yield {"channel": channel, "data": book(now[0]) if channel == "l2Book" else []}

    monkeypatch.setattr("glyde_mm.public_tape.time.time_ns", clock)
    path = tmp_path / "recorded.jsonl"
    await record_tape(config(), path, 1, include_hl=True, client_factory=Public)
    assert len(seen) == 2 and all(source.closed for source in seen)
    events = read_tape(path, "BTC")[1]
    assert {row.source for row in events} == {"glyde", "hyperliquid"}


async def test_failed_public_capture_retains_partial_file_and_closes_source(tmp_path):
    class Failed:
        closed = False

        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            Failed.closed = True

        async def meta(self):
            return {"universe": [{"name": "BTC", "szDecimals": 3}]}

        async def stream(self, subscriptions):
            yield {"channel": "l2Book", "data": book(1000)}
            raise OSError("synthetic feed failure")

    path = tmp_path / "partial.jsonl"
    with pytest.raises(ValueError, match="ended before coverage"):
        await record_tape(config(), path, 1, client_factory=Failed)
    assert path.exists() and Failed.closed


async def test_multi_market_config_is_refused_without_silently_choosing_one_asset(tmp_path):
    from glyde_mm.config import MarketConfig

    selected = Config(
        markets=(MarketConfig("ETH", D(1)),), risk=RiskConfig(max_total_notional=D(100))
    )
    with pytest.raises(ValueError, match="single-symbol"):
        await compare_strategies(selected, tape(tmp_path, market_events()))
    with pytest.raises(ValueError, match="single-symbol"):
        await record_tape(
            selected,
            tmp_path / "capture.jsonl",
            1,
            client_factory=lambda _: pytest.fail("must not construct a client"),
        )
