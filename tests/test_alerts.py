from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from dataclasses import replace
from decimal import Decimal as D

import httpx
import pytest

from glyde_mm import alerts, runner
from glyde_mm.config import Config
from glyde_mm.models import CancelResult, OrderRequest
from glyde_mm.paper import PaperExchange
from glyde_mm.risk import RiskViolation

WEBHOOK = "https://chat.invalid/private-token?key=never-log-this"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("alert tests must use the fake webhook server")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)
    monkeypatch.setenv("GLYDE_MM_ALERT_WEBHOOK", WEBHOOK)


class Webhook:
    def __init__(self, statuses=()):
        self.statuses = iter(statuses)
        self.received = []

    async def post(self, request):
        assert request.method == "POST" and str(request.url) == WEBHOOK
        self.received.append(json.loads(request.content))
        logging.getLogger("httpcore.http11").debug("reflected headers %s", WEBHOOK)
        return httpx.Response(next(self.statuses, 204), headers={"Location": WEBHOOK})

    def transport(self):
        return httpx.MockTransport(self.post)


async def test_disabled_alerts_do_not_read_env_or_construct_transport(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled alerts must not read URL or construct a client")

    monkeypatch.setattr(alerts, "_endpoint", forbidden)
    monkeypatch.setattr(alerts.httpx, "AsyncClient", forbidden)
    await alerts.notify_session(Config(), "run", "stopped", RiskViolation("daily_loss"))


@pytest.mark.parametrize(
    "error,events",
    [
        (None, ["session_end"]),
        (RiskViolation("max_total_notional"), ["risk_stop", "session_end"]),
        (RiskViolation("daily_loss"), ["risk_stop", "daily_loss_breach", "session_end"]),
        (
            RiskViolation("order_outcome_unknown"),
            ["risk_stop", "unknown_order_outcome", "session_end"],
        ),
        (
            RiskViolation("cancel_outcome_unknown"),
            ["risk_stop", "unknown_order_outcome", "session_end"],
        ),
    ],
)
async def test_fake_chat_receives_each_required_safe_event(error, events, caplog):
    caplog.set_level(logging.DEBUG)
    fake = Webhook()
    await alerts.notify_session(
        Config(alerts_enabled=True), "run-123", "risk_stop", error, transport=fake.transport()
    )
    assert [row["event"] for row in fake.received] == events
    assert all(
        row["run_id"] == "run-123" and row["text"].startswith("GLYDE-MM") for row in fake.received
    )
    assert WEBHOOK not in json.dumps(fake.received)
    assert "never-log-this" not in caplog.text
    assert "private-token" not in caplog.text
    logging.getLogger("httpx").info("unrelated public diagnostic remains enabled")
    assert "unrelated public diagnostic" in caplog.text


async def test_cause_chain_and_terminal_guard_reason_survive_transport_error():
    transport = httpx.ConnectError(WEBHOOK)
    cleanup = RiskViolation("cancel_outcome_unknown")
    cleanup.__cause__ = RiskViolation("daily_loss")
    cleanup.__context__ = cleanup
    assert alerts._events(cleanup, []) == [
        "risk_stop",
        "unknown_order_outcome",
        "daily_loss_breach",
        "session_end",
    ]
    assert alerts._events(transport, ["order_outcome_unknown"]) == [
        "risk_stop",
        "unknown_order_outcome",
        "session_end",
    ]


async def test_transient_retries_are_capped_and_can_recover(monkeypatch):
    sleeps = []

    async def backoff(delay):
        sleeps.append(delay)

    monkeypatch.setattr(alerts.asyncio, "sleep", backoff)
    fake = Webhook([500, 429, 204])
    reports = []
    await alerts.notify_session(
        Config(alerts_enabled=True),
        "run",
        "stopped",
        None,
        transport=fake.transport(),
        report=reports.append,
    )
    assert len(fake.received) == 3 and sleeps == [0.2, 0.4]
    assert not reports


@pytest.mark.parametrize("status,attempts", [(503, 3), (429, 3), (400, 1), (302, 1)])
async def test_no_unbounded_retries_or_redirects(status, attempts, monkeypatch):
    async def backoff(_):
        pass

    monkeypatch.setattr(alerts.asyncio, "sleep", backoff)
    fake = Webhook([status] * 10)
    reports = []
    await alerts.notify_session(
        Config(alerts_enabled=True),
        "run",
        "stopped",
        None,
        transport=fake.transport(),
        report=reports.append,
    )
    assert len(fake.received) == attempts
    assert len(reports) == 1 and "incomplete" in reports[0]
    assert WEBHOOK not in "".join(reports)


async def test_stalled_webhook_has_whole_delivery_deadline(monkeypatch):
    async def stalled(_):
        await asyncio.Event().wait()

    monkeypatch.setattr(alerts, "DELIVERY_SECONDS", 0.005)
    reports = []
    await alerts.notify_session(
        Config(alerts_enabled=True),
        "run",
        "stopped",
        None,
        transport=httpx.MockTransport(stalled),
        report=reports.append,
    )
    assert len(reports) == 1 and "incomplete" in reports[0]


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://chat.invalid/secret",
        "https://u:pass@chat.invalid/",
        "https://chat.invalid/#secret",
        "not-a-url",
    ],
)
async def test_enabled_invalid_or_missing_env_is_visible_without_echo(value, monkeypatch):
    monkeypatch.setenv("GLYDE_MM_ALERT_WEBHOOK", value)
    reports = []
    await alerts.notify_session(
        Config(alerts_enabled=True), "run", "stopped", None, report=reports.append
    )
    assert reports == ["Chat alerts unavailable: check the private webhook environment variable."]


def test_config_only_accepts_boolean_and_environment_variable_name():
    for values in ({"alerts_enabled": "true"}, {"alert_webhook_env": WEBHOOK}):
        with pytest.raises(ValueError) as caught:
            Config(**values)
        assert WEBHOOK not in str(caught.value)


@pytest.mark.parametrize("reason", ["unknown", "daily_loss"])
async def test_real_runner_alerts_after_cleanup_and_preserves_original_error(
    tmp_path,
    monkeypatch,
    reason,
):
    fake, cleaned, reports = Webhook(), [], []

    class Public:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def meta(self):
            return {"universe": [{"name": "BTC", "szDecimals": 4}]}

        async def stream(self, _):
            yield {
                "channel": "l2Book",
                "data": {
                    "coin": "BTC",
                    "time": time.time_ns() // 1_000_000,
                    "levels": [[{"px": "99", "sz": "2"}], [{"px": "101", "sz": "2"}]],
                },
            }
            await asyncio.Event().wait()

    class Paper(PaperExchange):
        account_reads = 0

        async def account(self):
            account = await super().account()
            self.account_reads += 1
            if reason == "daily_loss" and self.account_reads > 1:
                return replace(account, equity=account.equity - D(10))
            return account

        async def place_orders(self, orders):
            raise httpx.ReadError(WEBHOOK)

        async def cancel_all(self, symbol=None):
            cleaned.append(symbol)
            await super().cancel_all(symbol)

        async def cancel_orders(self, orders):
            return [
                CancelResult("already_closed", row.order_id, row.client_order_id) for row in orders
            ]

    class Strategy:
        def __init__(self, context):
            self.context = context

        async def on_book(self, _):
            await self.context.client.place_orders(
                [
                    OrderRequest("BTC", "buy", D("0.001"), D(99)),
                ]
            )

        async def stop(self):
            pass

    real_notify = alerts.notify_session

    async def notify(*args, **kwargs):
        assert cleaned and cleaned[-1] == "BTC"
        kwargs["transport"] = fake.transport()
        await real_notify(*args, **kwargs)

    monkeypatch.setattr(runner, "notify_session", notify)
    selected = Config(alerts_enabled=True, strategy_config={"order_amount": "0.001"})
    expected_error = httpx.ReadError if reason == "unknown" else RiskViolation
    with pytest.raises(expected_error):
        await runner.run(
            selected,
            state_directory=tmp_path,
            client_factory=lambda _: Public(),
            paper_factory=Paper,
            strategy_factory=lambda name, context, values: Strategy(context),
            report=reports.append,
        )
    expected = "unknown_order_outcome" if reason == "unknown" else "daily_loss_breach"
    assert [row["event"] for row in fake.received] == ["risk_stop", expected, "session_end"]
    assert WEBHOOK not in json.dumps(fake.received) + "".join(reports)


async def test_failed_webhook_does_not_replace_original_runner_error(tmp_path, monkeypatch):
    original = RiskViolation("daily_loss")

    async def session(*args, **kwargs):
        raise original

    async def refusal(request):
        raise httpx.ConnectError(WEBHOOK)

    real_notify = alerts.notify_session

    async def notify(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(refusal)
        await real_notify(*args, **kwargs)

    async def no_backoff(_):
        pass

    monkeypatch.setattr(alerts.asyncio, "sleep", no_backoff)
    monkeypatch.setattr(runner, "_run_session", session)
    monkeypatch.setattr(runner, "notify_session", notify)
    with pytest.raises(RiskViolation) as caught:
        await runner.run(
            Config(alerts_enabled=True), state_directory=tmp_path, report=lambda _: None
        )
    assert caught.value is original


@pytest.mark.parametrize("missing", [False, True])
async def test_failed_warning_sink_does_not_escape_or_print_secret(missing, monkeypatch, capsys):
    if missing:
        monkeypatch.delenv("GLYDE_MM_ALERT_WEBHOOK")

    async def refuse(_):
        raise httpx.ConnectError(WEBHOOK)

    async def no_backoff(_):
        pass

    def broken(_):
        raise BrokenPipeError(WEBHOOK)

    monkeypatch.setattr(alerts.asyncio, "sleep", no_backoff)
    await alerts.notify_session(
        Config(alerts_enabled=True),
        "run",
        "risk_stop",
        RiskViolation("daily_loss"),
        transport=httpx.MockTransport(refuse),
        report=broken,
    )
    output = capsys.readouterr()
    assert "Chat alert status unavailable" in output.err
    assert WEBHOOK not in output.out + output.err


def test_both_diagnostic_channels_can_fail_without_masking_outcome(monkeypatch):
    def broken(*args, **kwargs):
        raise BrokenPipeError(WEBHOOK)

    monkeypatch.setattr("builtins.print", broken)
    alerts._warn(broken, "delivery failed")
