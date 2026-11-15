"""Public fake-API checks and exact config-receipt live admission."""

import json
import socket
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal as D

import httpx
import pytest
import yaml

from glyde_mm import cli, market_preflight
from glyde_mm.client import OneApiClient
from glyde_mm.config import Config, MarketConfig, RiskConfig
from glyde_mm.market_preflight import MarketPreflightRequired, check, config_hash, require_preflight
from glyde_mm.runner import run
from glyde_mm.testing import FakeOneApiServer


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("preflight cannot sign, read keys or connect to the network")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)
    monkeypatch.setattr(Config, "secret", refused)
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "never-read-trading-value")
    monkeypatch.setenv("GLYDE_READ_API_KEY", "never-read-private-value")


def selected():
    return Config(
        mode="live",
        owner="0x" + "1" * 40,
        vault_address="0x" + "2" * 40,
        strategy_config={"order_amount": "0.1"},
        risk=RiskConfig(max_position=D(1), max_order_notional=D(25)),
    )


class Public:
    def __init__(self, metadata=None):
        self.fake = FakeOneApiServer(timestamp_ms=time.time_ns() // 1_000_000)
        self.fake.set_book("BTC", bids=[("99", "10")], asks=[("101", "10")])
        self.metadata = metadata
        self.requests = []

    def handle(self, request):
        body = json.loads(request.content)
        assert request.url.path.endswith("/info")
        assert body["type"] in {"meta", "l2Book"} and "user" not in body
        assert "X-Glyde-API-Key" not in request.headers
        self.requests.append(body)
        result = self.fake.info(body)
        if body["type"] == "meta":
            if self.metadata is not None:
                result = self.metadata
            else:
                result["universe"][0]["minOrderSize"] = "0.001"
        return httpx.Response(200, json=result)

    @asynccontextmanager
    async def factory(self, config):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as http:
            async with OneApiClient(config, http_client=http) as client:
                yield client


async def test_public_pass_records_exact_hash_without_private_or_signing(tmp_path):
    config = selected()
    public, lines = Public(), []
    assert await check(config, tmp_path, client_factory=public.factory, report=lines.append) == 0
    assert len(lines) == 1 and lines[0].startswith(f"PASS {config_hash(config)}")
    assert "GLYDE_API_PRIVATE_KEY=present" in lines[0] and "tick=" in lines[0]
    assert "never-read" not in lines[0]
    assert [r["type"] for r in public.requests] == ["meta", "l2Book"]
    require_preflight(config, tmp_path)
    receipt = next(tmp_path.glob("preflight-*.json"))
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert "never-read" not in receipt.read_text()


@pytest.mark.parametrize(
    "changes,fix",
    [
        ({"strategy_config": {"order_amount": "0.0001"}}, "order_amount must be at least"),
        ({"strategy_config": {"order_amount": "0.001001"}}, "order_amount must be a multiple"),
        ({"strategy_config": {"order_amount": "1"}}, "max_order_notional"),
        (
            {
                "strategy_config": {"order_amount": "0.1", "order_levels": 4},
                "risk": RiskConfig(max_position=D("0.3"), max_order_notional=D(25)),
            },
            "max_position",
        ),
        (
            {
                "strategy_config": {
                    "order_amount": "0.1",
                    "order_levels": 2,
                    "order_level_amount": "0.2",
                }
            },
            "level 2 quantity",
        ),
    ],
)
async def test_exact_configuration_fixes_no_receipt(tmp_path, changes, fix):
    config, lines = replace(selected(), **changes), []
    assert await check(config, tmp_path, client_factory=Public().factory, report=lines.append) == 1
    assert any(fix in line for line in lines)
    assert not any(line.startswith("PASS") for line in lines)
    with pytest.raises(MarketPreflightRequired):
        require_preflight(config, tmp_path)


@pytest.mark.parametrize(
    "market,fix",
    [
        ({"name": "BTC", "szDecimals": 5}, "minimum is unavailable"),
        ({"name": "BTC", "szDecimals": True}, "szDecimals"),
        ({"name": "BTC", "szDecimals": 5, "minOrderSize": "0"}, "minOrderSize must be positive"),
        ({"name": "BTC", "szDecimals": 5, "minOrderSize": "0.001", "tickSize": "7"}, "tickSize"),
        ({"name": "BTC", "szDecimals": 5, "minOrderSize": "NaN"}, "metadata"),
    ],
)
async def test_unknown_or_invalid_venue_rules_never_guess_pass(tmp_path, market, fix):
    lines = []
    assert (
        await check(
            selected(),
            tmp_path,
            client_factory=Public({"universe": [market]}).factory,
            report=lines.append,
        )
        == 1
    )
    assert any(fix in line for line in lines)
    if "minOrderSize" not in market:
        assert any("min_size=unavailable" in line for line in lines)
    assert not list(tmp_path.glob("preflight-*.json"))


async def test_names_iteration_never_reads_credential_values(tmp_path, monkeypatch):
    public = Public()
    # Construct HTTP transport before replacing the environment with a names-only mapping.
    async with httpx.AsyncClient(transport=httpx.MockTransport(public.handle)) as http:

        @asynccontextmanager
        async def factory(config):
            yield OneApiClient(config, http_client=http)

        class NamesOnly(Mapping):
            def __iter__(self):
                return iter(("GLYDE_API_PRIVATE_KEY", "GLYDE_READ_API_KEY"))

            def __len__(self):
                return 2

            def __getitem__(self, key):
                raise AssertionError("credential value accessed")

        with monkeypatch.context() as scope:
            scope.setattr(market_preflight.os, "environ", NamesOnly())
            lines = []
            assert (
                await check(selected(), tmp_path, client_factory=factory, report=lines.append) == 0
            )
            assert "GLYDE_READ_API_KEY=present" in lines[0]


async def test_every_config_change_revokes_pass_and_failed_refresh_removes_old_pass(tmp_path):
    config = selected()
    assert (
        await check(config, tmp_path, client_factory=Public().factory, report=lambda _: None) == 0
    )
    for changed in (
        replace(config, owner="0x" + "3" * 40),
        replace(config, mode="paper"),
        replace(config, base_url="https://other.invalid/v1"),
        replace(config, strategy_config={"order_amount": "0.2"}),
        replace(config, risk=replace(config.risk, max_position=D(2))),
    ):
        with pytest.raises(MarketPreflightRequired):
            require_preflight(changed, tmp_path)
    bad = Public({"universe": [{"name": "BTC", "szDecimals": 5}]})
    assert await check(config, tmp_path, client_factory=bad.factory, report=lambda _: None) == 1
    with pytest.raises(MarketPreflightRequired):
        require_preflight(config, tmp_path)


async def test_expired_future_or_forged_receipts_refuse(tmp_path):
    config = selected()
    assert (
        await check(config, tmp_path, client_factory=Public().factory, report=lambda _: None) == 0
    )
    path = next(tmp_path.glob("preflight-*.json"))
    original = json.loads(path.read_text())
    for changed in (
        {"checked_at": time.time() - 301},
        {"checked_at": time.time() + 10},
        {"passed": False},
        {"config_hash": "wrong"},
    ):
        path.write_text(json.dumps({**original, **changed}))
        with pytest.raises(MarketPreflightRequired):
            require_preflight(config, tmp_path)


async def test_missing_pass_refuses_runner_before_any_client_cleanup_or_key_read(tmp_path):
    with pytest.raises(MarketPreflightRequired):
        await run(
            selected(),
            state_directory=tmp_path,
            client_factory=lambda _: pytest.fail("live client constructed without PASS"),
            report=lambda _: None,
        )


async def test_multimarket_totals_and_skew_are_not_per_symbol_loopholes(tmp_path):
    config = replace(
        selected(),
        markets=(MarketConfig("BTC", D("0.3"), {"order_levels": 2}),),
        risk=replace(selected().risk, max_total_notional=D(15)),
    )
    lines = []
    assert await check(config, tmp_path, client_factory=Public().factory, report=lines.append) == 1
    assert any("max_total_notional" in line for line in lines)
    lines = []
    config = replace(
        selected(),
        strategy_config={"order_amount": "0.1", "order_levels": 2, "inventory_skew_enabled": True},
        risk=replace(selected().risk, max_position=D("0.3")),
    )
    assert await check(config, tmp_path, client_factory=Public().factory, report=lines.append) == 1
    assert any("including possible size skew" in line for line in lines)


def test_actual_cli_pass_and_changed_config_refusal(tmp_path, monkeypatch, capsys):
    config = tmp_path / "bot.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "mode": "live",
                "owner": selected().owner,
                "vault_address": selected().vault_address,
                "strategy_config": {"order_amount": "0.001"},
            }
        )
    )
    public = Public()
    monkeypatch.setattr(market_preflight, "OneApiClient", public.factory)
    assert cli.main(["preflight", str(config)]) == 0
    assert capsys.readouterr().out.startswith("PASS ")
    seen = []

    async def allowed(selected, *args, **kwargs):
        seen.append(config_hash(selected))

    monkeypatch.setattr("builtins.input", lambda _: "BTC")
    monkeypatch.setattr(cli, "run_with_signals", allowed)
    assert cli.main(["run", str(config)]) == 0
    assert len(seen) == 1
    config.write_text(config.read_text() + 'risk: {max_position: "0.2"}\n')
    monkeypatch.setattr("builtins.input", lambda _: "BTC")

    async def forbidden(*args, **kwargs):
        pytest.fail("changed config must not start the runner")

    monkeypatch.setattr(cli, "run_with_signals", forbidden)
    assert cli.main(["run", str(config)]) == 1
    assert "preflight" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["paper", "live"])
async def test_missing_environment_names_are_reported_without_values(tmp_path, monkeypatch, mode):
    monkeypatch.delenv("GLYDE_READ_API_KEY")
    lines = []
    result = await check(
        replace(selected(), mode=mode),
        tmp_path,
        client_factory=Public().factory,
        report=lines.append,
    )
    assert result == (1 if mode == "live" else 0)
    assert any("GLYDE_READ_API_KEY=absent" in line for line in lines)
    assert "never-read" not in "\n".join(lines)


async def test_stale_public_book_cannot_issue_a_pass(tmp_path):
    public, lines = Public(), []
    public.fake.now_ms -= 60000
    assert (
        await check(selected(), tmp_path, client_factory=public.factory, report=lines.append) == 1
    )
    assert any("fresh two-sided" in line for line in lines)
    assert not list(tmp_path.glob("preflight-*.json"))
