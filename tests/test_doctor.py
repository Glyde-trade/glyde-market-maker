from __future__ import annotations

import json
import socket

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


def install_public_info(monkeypatch, *, payload=None, failure=False):
    from glyde_mm import doctor

    calls = []

    def handle(request):
        assert request.url.path == "/v1/info"
        assert json.loads(request.content) == {"type": "meta"}
        assert "X-Glyde-API-Key" not in request.headers
        calls.append(request)
        if failure:
            raise httpx.ConnectError("never-print-transport-secret", request=request)
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
    assert len(calls) == 1


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
    assert len(calls) == 1


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
    assert len(calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy": "unknown-secret"},
        {"strategy_config": {"order_amount": "-1"}},
        {"strategy_config": {"hanging_orders_enabled": True}},
        {"risk": {"max_position": "-1"}},
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
    assert "test-value" not in output
    assert len(calls) == 1


def test_invalid_url_is_a_safe_public_failure(configuration, monkeypatch, capsys):
    from glyde_mm import doctor

    def unavailable(config):
        raise httpx.InvalidURL("never-print-url-secret")

    monkeypatch.setattr(doctor, "OneApiClient", unavailable)
    assert main(["doctor", str(configuration)]) == 1
    output = capsys.readouterr().out
    assert "Public info: FAIL" in output
    assert "secret" not in output
