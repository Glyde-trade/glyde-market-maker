from __future__ import annotations

import json
import time

import httpx
import pytest
from eth_account import Account as EthAccount
from eth_account.messages import encode_typed_data

from glyde_mm import client as client_module
from glyde_mm.client import OneApiClient
from glyde_mm.config import Config

# Ephemeral test-only wallet: no key material is committed or funded.
AGENT = EthAccount.create()
OWNER = "0x" + "22" * 20
VAULT = "0x" + "33" * 20


class FakeAgentSessions:
    def __init__(self) -> None:
        self.mints: list[dict] = []
        self.read_keys: list[str | None] = []
        self.ttl_ms = 60_000

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/one-api/session/agent"):
            self.mints.append(body)
            credential = f"gr1_synthetic_{len(self.mints)}"
            expires = int(time.time() * 1000) + self.ttl_ms
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"readCredential": credential, "expiresAt": expires},
                },
            )
        self.read_keys.append(request.headers.get("x-glyde-api-key"))
        return httpx.Response(200, json=[])


def recovered_signer(body: dict, *, chain: int) -> str:
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "GlydeReadSession": [
                {"name": "owner", "type": "address"},
                {"name": "agentAddress", "type": "address"},
                {"name": "issuedAt", "type": "uint64"},
                {"name": "nonce", "type": "uint64"},
            ],
        },
        "primaryType": "GlydeReadSession",
        "domain": {"name": "Glyde", "version": "1", "chainId": chain, "verifyingContract": VAULT},
        "message": {k: body[k] for k in ("owner", "agentAddress", "issuedAt", "nonce")},
    }
    sig = body["signature"]
    vrs = (sig["v"], int(sig["r"], 16), int(sig["s"], 16))
    return str(EthAccount.recover_message(encode_typed_data(full_message=typed), vrs=vrs))


@pytest.fixture
async def minting(monkeypatch):
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", AGENT.key.hex())
    monkeypatch.delenv("GLYDE_READ_API_KEY", raising=False)
    server = FakeAgentSessions()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as http:
        async with OneApiClient(
            Config(mode="live", owner=OWNER, vault_address=VAULT), http_client=http
        ) as api:
            yield api, server


async def test_absent_read_key_mints_with_the_agent_signature(minting):
    api, server = minting
    await api.info({"type": "openOrders", "user": OWNER})
    assert len(server.mints) == 1
    body = server.mints[0]
    assert set(body) == {"owner", "agentAddress", "issuedAt", "nonce", "signature"}
    assert body["owner"] == OWNER
    assert body["agentAddress"] == AGENT.address.lower()
    assert abs(body["issuedAt"] - time.time() * 1000) < 5_000
    assert recovered_signer(body, chain=42161) == AGENT.address
    assert server.read_keys == ["gr1_synthetic_1"]


async def test_minted_credential_is_reused_then_refreshed_before_expiry(minting):
    api, server = minting
    await api.info({"type": "openOrders", "user": OWNER})
    await api.info({"type": "clearinghouseState", "user": OWNER})
    assert len(server.mints) == 1
    # Inside the refresh margin the next read mints a new credential.
    server.ttl_ms = 0
    await api.read_key(fresh=True)
    await api.info({"type": "openOrders", "user": OWNER})
    assert len(server.mints) == 3
    assert server.mints[1]["nonce"] != server.mints[2]["nonce"]
    assert server.read_keys[-1] == "gr1_synthetic_3"


async def test_socket_connect_always_mints_fresh(minting):
    api, server = minting
    first = await api.read_key(fresh=True)
    second = await api.read_key(fresh=True)
    assert first != second
    assert len(server.mints) == 2


async def test_configured_read_key_never_mints(minting, monkeypatch):
    api, server = minting
    monkeypatch.setenv("GLYDE_READ_API_KEY", "synthetic-read-token")
    await api.info({"type": "openOrders", "user": OWNER})
    assert server.mints == []
    assert server.read_keys == ["synthetic-read-token"]
    assert not api.minted_reads()


async def test_minted_socket_rotates_before_lease(minting, monkeypatch):
    api, server = minting
    monkeypatch.setattr(client_module, "READ_SOCKET_ROTATE_SECONDS", 0)

    class Socket:
        def __init__(self) -> None:
            self.frames = [json.dumps({"channel": "pong"})] * 3

        async def send(self, _raw: str) -> None:
            return None

        async def close(self) -> None:
            return None

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            if not self.frames:
                raise StopAsyncIteration
            return self.frames.pop()

    headers: list[dict] = []

    class Connect:
        def __init__(self, url: str, **options) -> None:
            headers.append(options["additional_headers"])

        async def __aenter__(self) -> Socket:
            return Socket()

        async def __aexit__(self, *_: object) -> None:
            return None

    api._connect_ws = Connect
    with pytest.raises(client_module.StreamDisconnected):
        async for _ in api.stream([{"type": "orderUpdates", "user": OWNER}]):
            pass
    assert headers == [{"X-Glyde-API-Key": "gr1_synthetic_1"}]
