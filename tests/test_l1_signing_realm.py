"""L1 actions are signed in Glyde's realm "b" even when the config targets mainnet."""

import json

import httpx
from eth_account import Account
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

from glyde_mm.client import OneApiClient
from glyde_mm.config import Config

AGENT_KEY = "0x" + "4" * 64
VAULT = "0x298bd99847d8c2a71fa6218435c589d00e677a54"
OWNER = "0x40a330862802ed07f625fb9e83ac522abcfbe583"


async def test_mainnet_config_signs_l1_in_realm_b(monkeypatch):
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", AGENT_KEY)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "response": {"type": "default"}})

    config = Config(mode="live", owner=OWNER, vault_address=VAULT, mainnet=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with OneApiClient(config, http_client=http) as client:
            await client.exchange({"type": "cancel", "cancels": [{"a": 0, "o": 1}]})

    envelope = sent[0]
    agent = Account.from_key(AGENT_KEY).address.lower()
    recover = recover_agent_or_user_from_l1_action
    args = (envelope["action"], envelope["signature"], VAULT, envelope["nonce"], None)
    assert recover(*args, False).lower() == agent
    assert recover(*args, True).lower() != agent
