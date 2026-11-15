# Copyright 2026 Glyde. Apache-2.0; adapted from Hummingbot hyperliquid_perpetual.
"""Framework-independent exact-decimal wire and owner/namespace boundaries."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit

import eth_account
import msgpack
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_hex

ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
PRIVATE_TYPES = {
    "clearinghouseState",
    "openOrders",
    "frontendOpenOrders",
    "userFills",
    "userFillsByTime",
    "userFunding",
    "userNonFundingLedgerUpdates",
    "orderStatus",
    "userFees",
    "userRateLimit",
    "extraAgents",
    "userAbstraction",
}


def number(value):
    if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
        raise ValueError("use exact decimal strings")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid decimal") from None
    if not result.is_finite():
        raise ValueError("nonfinite decimal")
    return result


def canonical_order_id(value):
    if type(value) not in (int, str):
        raise ValueError("invalid order ID")
    text = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", text) or int(text) > 2**64 - 1:
        raise ValueError("invalid order ID")
    return text


def wire_number(value):
    result = number(value)
    if result <= 0:
        raise ValueError("price and size must be positive")
    return format(result, "f")


@dataclass(frozen=True)
class Settings:
    owner: str
    vault: str
    private_key: str = field(repr=False)
    read_credential: str = field(repr=False)
    base_url: str = "https://api.glyde.trade/v1"
    mainnet: bool = True

    def __post_init__(self):
        if any(
            not isinstance(s, str) or not ADDRESS.fullmatch(s) or int(s, 16) == 0
            for s in (self.owner, self.vault)
        ):
            raise ValueError("explicit registered owner and vault addresses required")
        if not isinstance(self.read_credential, str) or not self.read_credential.strip():
            raise ValueError("a separate Glyde read credential is required")
        if type(self.mainnet) is not bool:
            raise ValueError("mainnet must be boolean")
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path.rstrip("/") != "/v1"
            or (url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"})
        ):
            raise ValueError("base URL must be an explicit HTTPS /v1 endpoint (HTTP loopback only)")

    def url(self, path):
        if path not in {"/info", "/exchange"}:
            raise ValueError("unsupported One API route")
        return self.base_url.rstrip("/") + path

    @property
    def ws_url(self):
        url = urlsplit(self.base_url)
        return urlunsplit(
            (
                "wss" if url.scheme == "https" else "ws",
                url.netloc,
                url.path.rstrip("/") + "/ws",
                "",
                "",
            )
        )


class Wire:
    def __init__(self, settings):
        self.settings = settings
        try:
            self.wallet = eth_account.Account.from_key(settings.private_key)
        except Exception:
            raise ValueError("invalid Glyde trading key") from None
        self._nonce = 0
        self.last_envelope = None

    def info(self, body):
        body = dict(body)
        private = "user" in body or body.get("type") in PRIVATE_TYPES
        if private and body.get("user", "").lower() != self.settings.owner.lower():
            raise ValueError("private user must be the registered owner, never the vault")
        headers = {"X-Glyde-API-Key": self.settings.read_credential} if private else {}
        return body, headers

    def sign(self, action):
        if action.get("type") not in {
            "order",
            "cancel",
            "cancelByCloid",
            "batchModify",
            "updateLeverage",
        }:
            raise ValueError("unsupported trading action; money/grant domains are separate")
        nonce = max(time.time_ns() // 1_000_000, self._nonce + 1)
        self._nonce = nonce
        digest = keccak(
            msgpack.packb(action)
            + nonce.to_bytes(8, "big")
            + b"\x01"
            + bytes.fromhex(self.settings.vault[2:])
        )
        message = {
            "domain": {
                "chainId": 1337,
                "name": "Exchange",
                "version": "1",
                "verifyingContract": "0x" + "0" * 40,
            },
            "types": {
                "Agent": [
                    {"name": "source", "type": "string"},
                    {"name": "connectionId", "type": "bytes32"},
                ]
            },
            "primaryType": "Agent",
            "message": {"source": "a" if self.settings.mainnet else "b", "connectionId": digest},
        }
        signature = self.wallet.sign_message(encode_typed_data(full_message=message))
        result = {
            "action": action,
            "nonce": nonce,
            "vaultAddress": self.settings.vault,
            "signature": {"r": to_hex(signature.r), "s": to_hex(signature.s), "v": signature.v},
        }
        self.last_envelope = json.dumps(result, separators=(",", ":"))
        return self.last_envelope


def market_rules(meta, contexts=None):
    universe = meta["universe"]
    if not isinstance(universe, list) or len(universe) > 4096:
        raise ValueError("invalid market metadata")
    if contexts is not None and len(contexts) != len(universe):
        raise ValueError("market context count mismatch")
    result = []
    for asset, row in enumerate(universe):
        size = row["szDecimals"]
        if type(size) is not int or not 0 <= size <= 6 or not isinstance(row["name"], str):
            raise ValueError("invalid size precision")
        if contexts is not None:
            if number(contexts[asset]["markPx"]) <= 0:
                raise ValueError("invalid mark price")
        if not row.get("isDelisted", False):
            result.append(
                (row["name"], asset, Decimal(1).scaleb(-size), Decimal(1).scaleb(-(6 - size)))
            )
    if len({row[0] for row in result}) != len(result):
        raise ValueError("duplicate market symbol")
    return result


def quantize_price(price, size_decimals):
    price = number(price)
    if price <= 0:
        raise ValueError("price must be positive")
    if price == price.to_integral_value():
        return price
    tick = max(Decimal(1).scaleb(price.adjusted() - 4), Decimal(1).scaleb(-(6 - size_decimals)))
    return (price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
