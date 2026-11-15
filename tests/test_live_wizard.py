from __future__ import annotations

import asyncio
import json
import signal
import socket
import time
from dataclasses import replace
from decimal import Decimal as D

import pytest
import yaml

from glyde_mm import cli
from glyde_mm.config import Config, MarketConfig, RiskConfig, load_config
from glyde_mm.live_wizard import conservative_config, first_live_session
from glyde_mm.models import Book, Level
from glyde_mm.risk import RiskViolation


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("wizard tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "fixture-key")
    monkeypatch.setenv("GLYDE_READ_API_KEY", "fixture-read")


class Public:
    reads = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def meta(self):
        self.reads.append("meta")
        return {"universe": [{"name": "BTC", "szDecimals": 4, "minOrderSize": "0.0001"}]}

    async def book(self, symbol):
        self.reads.append("book")
        return Book(
            symbol, (Level(D(99), D(5)),), (Level(D(101), D(5)),), time.time_ns() // 1_000_000
        )


def config():
    return Config(
        mode="live",
        owner="0x" + "1" * 40,
        vault_address="0x" + "2" * 40,
        strategy_config={"order_amount": "1", "order_levels": 3},
        risk=RiskConfig(
            max_position=D(2),
            max_order_notional=D(100),
            max_daily_loss=D(10),
            max_orders_per_minute=100,
        ),
    )


def config_file(tmp_path):
    path = tmp_path / "bot.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "mode": "live",
                "symbol": "BTC",
                "owner": "0x" + "1" * 40,
                "vault_address": "0x" + "2" * 40,
            }
        )
    )
    return path


def report(tmp_path):
    paths = list((tmp_path / ".glyde-mm" / "live-wizard").glob("session-*/report.json"))
    assert len(paths) == 1
    assert paths[0].stat().st_mode & 0o777 == 0o600
    return json.loads(paths[0].read_text())


async def test_smallest_preset_caps_without_enlarging_tighter_limits():
    base = config()
    selected = await conservative_config(base, client_factory=lambda _: Public())
    assert selected.strategy_config["order_levels"] == 1
    assert selected.strategy_config["order_refresh_time"] == 60
    assert selected.risk.max_position == D(".25")
    assert selected.risk.max_order_notional == selected.risk.max_total_notional == D(25)
    assert selected.risk.max_daily_loss == D(5)
    assert selected.risk.max_orders_per_minute == 60
    small = replace(
        base,
        risk=replace(
            base.risk,
            max_position=D(".1"),
            max_daily_loss=D(1),
            max_order_notional=D(2),
            max_total_notional=D(3),
            max_orders_per_minute=7,
        ),
    )
    smaller = await conservative_config(small, client_factory=lambda _: Public())
    assert smaller.risk == small.risk
    assert base.strategy_config["order_levels"] == 3


@pytest.mark.parametrize("change", ["paper", "multi", "old", "precision", "tiny"])
async def test_invalid_or_unavailable_setup_cannot_start_preview(change):
    selected = config()

    class Broken(Public):
        async def book(self, symbol):
            book = await super().book(symbol)
            return replace(book, timestamp_ms=1) if change == "old" else book

        async def meta(self):
            result = await super().meta()
            if change == "precision":
                result["universe"][0]["szDecimals"] = True
            return result

    if change == "paper":
        selected = replace(selected, mode="paper")
    if change == "multi":
        selected = replace(
            selected,
            markets=(MarketConfig("ETH", D(1)),),
            risk=replace(selected.risk, max_total_notional=D(25)),
        )
    if change == "tiny":
        selected = replace(selected, risk=replace(selected.risk, max_position=D(".00000001")))
    with pytest.raises(ValueError):
        await conservative_config(selected, client_factory=lambda _: Broken())


async def test_exact_config_two_minute_preview_confirmation_then_30_minute_live(tmp_path):
    path = config_file(tmp_path)
    events, sessions, printed = [], [], []

    async def doctor(effective_path, *, private, report):
        assert private is True
        assert effective_path.stat().st_mode & 0o777 == 0o600
        effective = load_config(effective_path)
        assert effective.mode == "live" and effective.strategy_config["order_levels"] == 1
        events.append("doctor")
        return 0

    async def session(selected, directory, *, duration_seconds, log_directory):
        assert directory == tmp_path / ".glyde-mm"
        assert log_directory.is_relative_to(directory / "live-wizard")
        sessions.append((selected, duration_seconds))
        events.append(selected.mode)
        # Changing the user's file after preview must not alter the live session.
        if selected.mode == "paper":
            path.write_text("mode: live\nsymbol: ETH\n")
        return True

    def confirmation(_):
        events.append("confirm")
        return "BTC"

    assert (
        await first_live_session(
            path,
            run_session=session,
            input_fn=confirmation,
            client_factory=lambda _: Public(),
            doctor_fn=doctor,
            report=printed.append,
        )
        == 0
    )
    assert events == ["doctor", "paper", "confirm", "live"]
    assert [seconds for _, seconds in sessions] == [120, 1800]
    assert replace(sessions[0][0], mode="live") == sessions[1][0]
    result = report(tmp_path)
    assert result["outcome"] == "auto_stopped"
    assert result["live_requested"] is True
    assert result["runner_exit_without_cleanup_error"] is True
    assert result["positions_closed"] is False
    assert "FIRST SESSION auto_stopped" in printed[-1]


@pytest.mark.parametrize("failure", ["doctor", "preview", "short_preview", "confirmation", "eof"])
async def test_refusals_never_reach_live_and_always_report(tmp_path, failure):
    calls = []

    async def doctor(*args, **kwargs):
        return 1 if failure == "doctor" else 0

    async def session(selected, *args, **kwargs):
        calls.append(selected.mode)
        if failure == "preview":
            raise RiskViolation("stale_book")
        return failure != "short_preview"

    def confirmation(_):
        if failure == "eof":
            raise EOFError
        return "wrong"

    assert (
        await first_live_session(
            config_file(tmp_path),
            run_session=session,
            input_fn=confirmation,
            client_factory=lambda _: Public(),
            doctor_fn=doctor,
            report=lambda _: None,
        )
        == 1
    )
    assert "live" not in calls
    assert report(tmp_path)["live_requested"] is False


async def test_live_cleanup_failure_is_not_reported_as_stopped(tmp_path):
    async def doctor(*args, **kwargs):
        return 0

    async def session(selected, *args, **kwargs):
        if selected.mode == "live":
            raise RiskViolation("cancel_outcome_unknown")
        return True

    assert (
        await first_live_session(
            config_file(tmp_path),
            run_session=session,
            input_fn=lambda _: "BTC",
            client_factory=lambda _: Public(),
            doctor_fn=doctor,
            report=lambda _: None,
        )
        == 1
    )
    result = report(tmp_path)
    assert result["outcome"] == "failed" and result["phase"] == "live"
    assert result["runner_exit_without_cleanup_error"] is False


async def test_timed_runner_deadline_sets_raw_stop_and_restores_signals(tmp_path, monkeypatch):
    events = []
    before = {kind: signal.getsignal(kind) for kind in (signal.SIGINT, signal.SIGTERM)}

    async def runner(selected, *, stop_event, state_directory, allow_existing_exposure, **kwargs):
        assert not allow_existing_exposure
        await stop_event.wait()
        events.append("stopped")

    monkeypatch.setattr(cli, "run_strategy", runner)
    assert await cli.run_with_signals(config(), tmp_path, duration_seconds=0.001) is True
    assert events == ["stopped"]
    assert all(signal.getsignal(kind) == handler for kind, handler in before.items())


async def test_early_runner_exit_is_not_a_completed_preview(tmp_path, monkeypatch):
    async def runner(*args, **kwargs):
        pass

    monkeypatch.setattr(cli, "run_strategy", runner)
    assert await cli.run_with_signals(config(), tmp_path, duration_seconds=120) is False


@pytest.mark.parametrize("interrupted", [False, True])
async def test_preview_interruption_wins_over_timer_during_cleanup(
    tmp_path, monkeypatch, interrupted
):
    async def runner(*args, **kwargs):
        if interrupted:
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        await kwargs["stop_event"].wait()
        # Model slow cancellation/reporting; the original countdown expires here.
        await asyncio.sleep(0.02)

    monkeypatch.setattr(cli, "run_strategy", runner)
    result = await cli.run_with_signals(config(), tmp_path, duration_seconds=0.005)
    assert result is (not interrupted)


def test_live_wizard_cli_routes_without_existing_run_confirmation(tmp_path, monkeypatch):
    calls = []

    async def wizard(path, *, run_session):
        calls.append((path, run_session))
        return 1

    monkeypatch.setattr("glyde_mm.live_wizard.first_live_session", wizard)
    assert cli.main(["live-wizard", str(tmp_path / "bot.yaml")]) == 1
    assert calls == [(tmp_path / "bot.yaml", cli.run_with_signals)]


async def test_real_fake_client_paper_then_signed_live_and_timer_cleanup(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    import httpx
    from eth_account import Account as Wallet
    from live_fake import SequencedFake

    from glyde_mm import doctor
    from glyde_mm.client import OneApiClient
    from glyde_mm.runner import run

    wallet = Wallet.create()
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", wallet.key.hex())
    monkeypatch.setenv("GLYDE_READ_API_KEY", "synthetic-read-only")
    sequenced = SequencedFake(read_credential="synthetic-read-only")
    fake = sequenced.ledger
    fake.set_book("BTC", bids=[("99", "2")], asks=[("101", "2")])
    actions, stages = [], []

    @asynccontextmanager
    async def factory(selected):
        transport = fake.http_transport()

        async def observe(request):
            if request.url.path.endswith("/exchange"):
                assert selected.mode == "live" and stages[-1] == "live"
                envelope = json.loads(request.content)
                assert envelope["signature"] and envelope["vaultAddress"] == selected.vault_address
                actions.append(envelope["action"]["type"])
            if json.loads(request.content).get("type") == "meta":
                metadata = fake.info({"type": "meta"})
                metadata["universe"][0]["minOrderSize"] = "0.00001"
                return httpx.Response(200, json=metadata)
            if selected.mode == "live":
                return sequenced.http(request)
            return await transport.handle_async_request(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(observe)) as http:
            async with OneApiClient(
                selected,
                http_client=http,
                connect_ws=sequenced.connect if selected.mode == "live" else fake.connect_ws,
            ) as client:
                yield client

    async def actual_runner(selected, **kwargs):
        return await run(selected, client_factory=factory, **kwargs)

    monkeypatch.setattr(cli, "run_strategy", actual_runner)
    monkeypatch.setattr(doctor, "OneApiClient", factory)

    async def session(selected, directory, *, duration_seconds, log_directory):
        stages.append(selected.mode)
        assert duration_seconds == (120 if selected.mode == "paper" else 1800)
        if selected.mode == "paper":
            assert not actions
        # Only this fake fixture shortens time; production constants/CLI have no override.
        return await cli.run_with_signals(
            selected,
            directory,
            duration_seconds=0.05 if selected.mode == "paper" else 0.2,
            log_directory=log_directory,
        )

    assert (
        await first_live_session(
            config_file(tmp_path),
            run_session=session,
            input_fn=lambda _: "BTC",
            client_factory=factory,
            report=lambda _: None,
        )
        == 0
    )
    assert stages == ["paper", "live"]
    assert actions == ["order", "cancel", "cancel"]
    assert sequenced.sequence == 4  # Baseline plus each signed order/cancel commit.
    assert {sub["type"] for sub in sequenced.socket.subscriptions} == {
        "l2Book",
        "clearinghouseState",
        "openOrders",
        "orderUpdates",
        "userFills",
    }
    assert not fake.info({"type": "openOrders", "user": fake.user})
    result = report(tmp_path)
    assert result["outcome"] == "auto_stopped"
    assert len(result["diagnostics"]["runs"]) == 2
