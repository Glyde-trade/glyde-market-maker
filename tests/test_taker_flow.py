"""Local-only development helper, real SDK signing and matching, no network."""

import importlib.util
import json
import socket
from contextlib import asynccontextmanager
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from eth_account import Account
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

from glyde_mm.client import OneApiClient, OneApiError, OutcomeUnknown
from glyde_mm.config import Config
from glyde_mm.testing import FakeOneApiServer

spec = importlib.util.spec_from_file_location(
    "taker_flow", Path(__file__).parents[1] / "examples/dev/taker_flow.py"
)
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*_args, **_kwargs):
        pytest.fail("offline helper tests cannot connect")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.mark.parametrize(
    "origin",
    [
        "https://api.glyde.trade/v1",
        "https://example.invalid",
        "http://127.0.0.1.evil.invalid",
        "http://user:pass@localhost:8080",
        "http://localhost:8080?token=secret",
        "http://[::2]:8080",
    ],
)
def test_refuses_remote_or_credential_bearing_origins(origin):
    with pytest.raises(ValueError, match="non-loopback"):
        flow.require_loopback(origin)


@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:8080/v1", "http://[::1]:8080/v1", "https://localhost:8080/v1"]
)
def test_accepts_explicit_loopback(origin):
    flow.require_loopback(origin)


async def test_real_signed_ioc_orders_create_native_shaped_public_trades_without_resting(
    tmp_path, monkeypatch
):
    wallet = Account.create()
    monkeypatch.setenv("LOCAL_TAKER_KEY", wallet.key.hex())
    selected = Config(
        mode="live",
        base_url="http://127.0.0.1:8080/v1",
        owner=wallet.address,
        vault_address="0x" + "22" * 20,
        private_key_env="LOCAL_TAKER_KEY",
    )
    fake = FakeOneApiServer()
    fake.set_book("BTC", bids=[("99", "2")], asks=[("101", "2")])
    actions, messages = [], []

    @asynccontextmanager
    async def factory(config):
        transport = fake.http_transport()

        async def handle(request):
            body = json.loads(request.content)
            if request.url.path.endswith("/exchange"):
                actions.append(body)
                assert (
                    recover_agent_or_user_from_l1_action(
                        body["action"],
                        body["signature"],
                        selected.vault_address,
                        body["nonce"],
                        None,
                        True,
                    )
                    == wallet.address
                )
            return await transport.handle_async_request(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            async with OneApiClient(config, http_client=http) as client:
                yield client

    async def no_sleep(_):
        pass

    result = await flow.run_flow(
        selected,
        count=4,
        quantity=D(".01"),
        interval=3,
        client_factory=factory,
        sleep=no_sleep,
        report=messages.append,
    )
    assert result["attempts"] == result["filled"] == 4
    assert len(actions) == 4 and len({row["action"]["orders"][0]["c"] for row in actions}) == 4
    assert [row["action"]["orders"][0]["b"] for row in actions].count(True) == 2
    assert all(row["action"]["orders"][0]["t"] == {"limit": {"tif": "Ioc"}} for row in actions)
    assert not fake.info({"type": "openOrders", "user": fake.user})
    assert len(fake.info({"type": "userFills", "user": fake.user})) == 4
    assert sum(line.startswith("SUBMIT original=") for line in messages) == 4


async def test_remote_fails_before_client_or_credentials(monkeypatch):
    monkeypatch.setattr(Config, "secret", lambda *_: pytest.fail("must not read credentials"))
    with pytest.raises(ValueError, match="non-loopback"):
        await flow.run_flow(
            Config(mode="live"), client_factory=lambda _: pytest.fail("must not open client")
        )


@pytest.mark.parametrize("refusal", [OutcomeUnknown("unknown"), OneApiError("refused")])
async def test_unknown_or_refused_original_is_never_retried(refusal):
    class Client:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def meta(self):
            return {"universe": [{"name": "BTC", "szDecimals": 5}]}

        async def book(self, _):
            from glyde_mm.models import Book, Level

            return Book("BTC", (Level(D(99), D(2)),), (Level(D(101), D(2)),), 1)

        async def place_orders(self, orders):
            self.calls += 1
            raise refusal

    client, messages = Client(), []
    with pytest.raises(type(refusal)):
        await flow.run_flow(
            Config(mode="live", base_url="http://127.0.0.1:8080/v1"),
            client_factory=lambda _: client,
            report=messages.append,
        )
    assert client.calls == 1
    assert messages[0].startswith("SUBMIT original=0x")


async def test_notional_budget_refuses_before_dispatch():
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def meta(self):
            return {"universe": [{"name": "BTC", "szDecimals": 5}]}

        async def book(self, _):
            from glyde_mm.models import Book, Level

            return Book("BTC", (Level(D(99), D(2)),), (Level(D(101), D(2)),), 1)

        async def place_orders(self, _):
            pytest.fail("over budget")

    with pytest.raises(ValueError, match="budget exhausted"):
        await flow.run_flow(
            Config(mode="live", base_url="http://localhost:8080/v1"),
            quantity=D(1),
            client_factory=lambda _: Client(),
        )
