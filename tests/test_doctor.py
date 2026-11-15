from __future__ import annotations

import json
import socket
import time

import httpx
import pytest
import yaml

from glyde_mm.cli import main
from glyde_mm.client import OneApiClient
from glyde_mm.wizard import run_wizard


@pytest.fixture
def configuration(tmp_path):
    path = run_wizard(tmp_path / "bot.yaml", interactive=False, print_fn=lambda _: None)
    return path


def install_public_info(
    monkeypatch, *, payload=None, failure=False, status=200, at="meta", date=None
):
    from glyde_mm import doctor

    calls = []

    def handle(request):
        assert request.url.path == "/v1/info"
        body = json.loads(request.content)
        assert body["type"] in {"meta", "l2Book"}
        assert "X-Glyde-API-Key" not in request.headers
        calls.append(request)
        if body["type"] == at and status != 200:
            return httpx.Response(status, json={"error": "never-print-response-secret"})
        if failure:
            raise httpx.ConnectError("never-print-transport-secret", request=request)
        if body["type"] == "l2Book":
            return httpx.Response(
                200,
                headers={"Date": date} if date is not None else {},
                json={
                    "coin": "BTC",
                    "time": time.time_ns() // 1_000_000,
                    "levels": [[{"px": "99999", "sz": "1"}], [{"px": "100001", "sz": "1"}]],
                },
            )
        return httpx.Response(
            200,
            json=payload
            if payload is not None
            else {
                "universe": [{"name": "BTC", "szDecimals": 5}],
            },
        )

    def connect_forbidden(*args, **kwargs):
        raise AssertionError("doctor must not open sockets outside the injected public transport")

    monkeypatch.setattr(socket.socket, "connect", connect_forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_forbidden)

    class PublicClient(OneApiClient):
        def __init__(self, config):
            super().__init__(
                config,
                http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(handle),
                ),
            )

        async def close(self):
            await self._http.aclose()

    monkeypatch.setattr(doctor, "OneApiClient", PublicClient)
    monkeypatch.delenv("GLYDE_API_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GLYDE_READ_API_KEY", raising=False)
    return calls


def test_paper_doctor_checks_public_info_once_without_keys(configuration, monkeypatch, capsys):
    calls = install_public_info(monkeypatch)
    assert main(["doctor", str(configuration)]) == 0
    output = capsys.readouterr().out
    assert "Configuration: OK (paper)" in output
    assert "Trading key: absent (optional in paper mode)" in output
    assert "Read key: absent (optional in paper mode)" in output
    assert "Public info: OK" in output
    assert len(calls) == 2


def test_live_missing_keys_fails_but_still_checks_public_connectivity(
    configuration,
    monkeypatch,
    capsys,
):
    values = yaml.safe_load(configuration.read_text())
    values.update(mode="live", owner="0x" + "1" * 40, vault_address="0x" + "2" * 40)
    configuration.write_text(yaml.safe_dump(values))
    calls = install_public_info(monkeypatch)
    assert main(["doctor", "--config", str(configuration)]) == 1
    output = capsys.readouterr().out
    assert "Trading key: absent (required in live mode)" in output
    assert "Read key: absent (required in live mode)" in output
    assert "Public info: OK" in output
    assert len(calls) == 2


def test_key_presence_never_prints_values_or_authorizes_private_calls(
    configuration,
    monkeypatch,
    capsys,
):
    values = yaml.safe_load(configuration.read_text())
    values.update(mode="live", owner="0x" + "1" * 40, vault_address="0x" + "2" * 40)
    configuration.write_text(yaml.safe_dump(values))
    calls = install_public_info(monkeypatch)
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "DO-NOT-PRINT-SIGNING")
    monkeypatch.setenv("GLYDE_READ_API_KEY", "DO-NOT-PRINT-READ")
    assert main(["doctor", str(configuration)]) == 0
    output = capsys.readouterr().out
    assert "Trading key: present" in output
    assert "Read key: present" in output
    assert "DO-NOT-PRINT" not in output
    assert len(calls) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy": "unknown-secret"},
        {"strategy_config": {"order_amount": "-1"}},
        {"strategy_config": {"hanging_orders_enabled": True}},
        {"risk": {"max_position": "-1"}},
        {"risk": {"stale_book_seconds": "3601"}},
        {"risk": {"account_age_seconds": "3601"}},
        {"mode": "live", "owner": "invalid-owner-secret"},
    ],
)
def test_invalid_configuration_refuses_before_connectivity(
    changes,
    configuration,
    monkeypatch,
    capsys,
):
    calls = install_public_info(monkeypatch)
    values = yaml.safe_load(configuration.read_text())
    values.update(changes)
    configuration.write_text(yaml.safe_dump(values))
    assert main(["doctor", str(configuration)]) == 1
    output = capsys.readouterr().out
    assert "Configuration: FAIL" in output
    assert "secret" not in output
    assert calls == []


@pytest.mark.parametrize(
    "payload,failure",
    [
        (None, True),
        ({"universe": []}, False),
        ({"universe": [{"name": "BTC", "szDecimals": True}]}, False),
        ({"universe": [{"name": "BTC", "szDecimals": 7}]}, False),
        ({"universe": "invalid-secret"}, False),
    ],
)
def test_unavailable_or_malformed_public_info_fails_safely(
    payload,
    failure,
    configuration,
    monkeypatch,
    capsys,
):
    calls = install_public_info(monkeypatch, payload=payload, failure=failure)
    assert main(["doctor", str(configuration)]) == 1
    output = capsys.readouterr().out
    assert "Public info: FAIL" in output
    assert "secret" not in output
    assert len(calls) == 1


def test_doctor_missing_config_is_a_safe_failure(tmp_path, capsys):
    assert main(["doctor", str(tmp_path / "missing-private-path.yaml")]) == 1
    output = capsys.readouterr().out
    assert "Configuration: FAIL" in output
    assert str(tmp_path) not in output


def test_default_config_and_custom_key_names(configuration, monkeypatch, capsys):
    calls = install_public_info(monkeypatch)
    values = yaml.safe_load(configuration.read_text())
    values.update(private_key_env="CUSTOM_TRADING_KEY", read_api_key_env="CUSTOM_READ_KEY")
    configuration.write_text(yaml.safe_dump(values))
    configuration.rename(configuration.parent / "glyde-mm.yaml")
    monkeypatch.chdir(configuration.parent)
    monkeypatch.setenv("CUSTOM_TRADING_KEY", "private-test-value")
    monkeypatch.setenv("CUSTOM_READ_KEY", "read-test-value")
    assert main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "Trading key: present" in output
    assert "Read key: present" in output
    assert "[variable: CUSTOM_TRADING_KEY]" in output
    assert "[variable: CUSTOM_READ_KEY]" in output
    assert "test-value" not in output
    assert len(calls) == 2


@pytest.mark.parametrize("offset,failed", [(0, False), (15, True), (-15, True)])
def test_public_http_date_detects_clock_skew_without_extra_or_private_requests(
    configuration, monkeypatch, capsys, offset, failed
):
    from email.utils import formatdate

    calls = install_public_info(monkeypatch, date=formatdate(time.time() + offset, usegmt=True))
    assert main(["doctor", str(configuration)]) == int(failed)
    output = capsys.readouterr().out
    assert "Clock skew: " + ("FAIL" if failed else "no detected skew") in output
    assert len(calls) == 2


@pytest.mark.parametrize("date", [None, "invalid-private-value"])
def test_missing_invalid_clock_header_is_explicitly_unavailable(
    configuration, monkeypatch, capsys, date
):
    calls = install_public_info(monkeypatch, date=date)
    assert main(["doctor", str(configuration)]) == 0
    output = capsys.readouterr().out
    assert "Clock skew: unavailable" in output
    assert "invalid-private-value" not in output
    assert len(calls) == 2


def test_clock_bounds_include_request_latency_and_second_rounding():
    from glyde_mm.doctor import check_clock

    lines = []
    assert check_clock((100, 103, 101), lines.append)
    assert not check_clock((103, 100, 101), lines.append)
    assert "local wall clock changed" in lines[-1]
    assert not check_clock((float("nan"), 100, 101), lines.append)
    assert "invalid clock observation" in lines[-1]


def test_invalid_url_is_a_safe_public_failure(configuration, monkeypatch, capsys):
    from glyde_mm import doctor

    def unavailable(config):
        raise httpx.InvalidURL("never-print-url-secret")

    monkeypatch.setattr(doctor, "OneApiClient", unavailable)
    assert main(["doctor", str(configuration)]) == 1
    output = capsys.readouterr().out
    assert "Public info: FAIL" in output
    assert "secret" not in output


def test_market_precision_and_tick_are_distinct_from_unavailable_minima(
    configuration, monkeypatch, capsys
):
    install_public_info(monkeypatch)
    assert main(["doctor", str(configuration)]) == 0
    output = capsys.readouterr().out
    assert "Quantity step: 0.00001" in output
    assert "Runner price tick at current midpoint: 10" in output
    assert "Venue minimum order size/notional: unavailable" in output
    assert "Signing/trading permissions and live execution are not verified" in output


@pytest.mark.parametrize("refused", [False, True])
def test_explicit_live_private_check_only_reads_never_signs_or_prints_account(
    configuration, monkeypatch, capsys, refused
):
    from glyde_mm import doctor
    from glyde_mm.client import EthAccount

    values = yaml.safe_load(configuration.read_text())
    values.update(mode="live", owner="0x" + "1" * 40, vault_address="0x" + "2" * 40)
    configuration.write_text(yaml.safe_dump(values))
    calls = []

    def handle(request):
        assert request.url.path == "/v1/info"
        kind = json.loads(request.content)["type"]
        calls.append(kind)
        if kind in {"openOrders", "clearinghouseState"}:
            assert request.headers["X-Glyde-API-Key"] == "private-read-value"
            if refused:
                return httpx.Response(403, json={"error": "NEVER_PRINT_ACCOUNT_SECRET"})
            if kind == "openOrders":
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json={
                    "marginSummary": {"accountValue": "987654321.12"},
                    "withdrawable": "12345678.90",
                    "assetPositions": [],
                },
            )
        assert "X-Glyde-API-Key" not in request.headers
        if kind == "meta":
            return httpx.Response(200, json={"universe": [{"name": "BTC", "szDecimals": 5}]})
        assert kind == "l2Book"
        return httpx.Response(
            200,
            json={
                "coin": "BTC",
                "time": time.time_ns() // 1_000_000,
                "levels": [[{"px": "99", "sz": "1"}], [{"px": "101", "sz": "1"}]],
            },
        )

    class ReadClient(OneApiClient):
        def __init__(self, config):
            super().__init__(
                config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
            )

        async def close(self):
            await self._http.aclose()

    def forbidden(*args, **kwargs):
        raise AssertionError("doctor must never construct a signing wallet")

    monkeypatch.setattr(EthAccount, "from_key", forbidden)
    monkeypatch.setattr(doctor, "OneApiClient", ReadClient)
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "not-a-real-signing-key")
    monkeypatch.setenv("GLYDE_READ_API_KEY", "private-read-value")
    assert main(["doctor", str(configuration), "--private"]) == int(refused)
    output = capsys.readouterr().out
    assert "Private reads: " + ("FAIL" if refused else "OK") in output
    assert not any(
        value in output
        for value in (
            "not-a-real-signing-key",
            "private-read-value",
            "987654321",
            "12345678",
            "NEVER_PRINT",
        )
    )
    assert calls == ["meta", "l2Book", "clearinghouseState"] + ([] if refused else ["openOrders"])


def test_private_check_refuses_paper_mode_before_any_network(configuration, monkeypatch, capsys):
    calls = install_public_info(monkeypatch)
    assert main(["doctor", str(configuration), "--private"]) == 1
    assert "Configuration: FAIL" in capsys.readouterr().out
    assert calls == []


def test_stale_public_book_fails_without_private_reads(configuration, monkeypatch, capsys):
    from decimal import Decimal

    from glyde_mm import doctor
    from glyde_mm.models import Book, Level

    calls = install_public_info(monkeypatch)

    async def old_book(self, symbol):
        return Book("BTC", (Level(Decimal(99), Decimal(1)),), (Level(Decimal(101), Decimal(1)),), 1)

    monkeypatch.setattr(doctor.OneApiClient, "book", old_book)
    assert main(["doctor", str(configuration)]) == 1
    assert "Public info: FAIL" in capsys.readouterr().out
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 404])
@pytest.mark.parametrize("at", ["meta", "l2Book"])
def test_old_public_surface_reports_one_api_not_served(
    configuration, monkeypatch, capsys, status, at
):
    calls = install_public_info(monkeypatch, status=status, at=at)
    assert main(["doctor", str(configuration)]) == 1
    output = capsys.readouterr().out
    assert f"requested One API read unavailable at this endpoint: HTTP {status}" in output
    assert "One API may not be deployed yet" in output
    assert "secret" not in output
    assert len(calls) == (1 if at == "meta" else 2)


@pytest.mark.parametrize("status", [403, 429, 503])
def test_other_public_failures_keep_generic_diagnostic(configuration, monkeypatch, capsys, status):
    calls = install_public_info(monkeypatch, status=status)
    assert main(["doctor", str(configuration)]) == 1
    output = capsys.readouterr().out
    assert "Public info: FAIL (unavailable" in output
    assert "One API may not be deployed yet" not in output
    assert "secret" not in output
    assert len(calls) == 1
