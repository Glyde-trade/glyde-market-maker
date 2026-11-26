"""Async One API transport using the official Hyperliquid SDK signing primitive."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import secrets
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from eth_account import Account as EthAccount
from eth_account.messages import encode_typed_data
from hyperliquid.utils.signing import sign_l1_action
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from .config import Config, decimal
from .models import (
    Account,
    Book,
    CancelRequest,
    CancelResult,
    HistoricalOrder,
    Level,
    ModifyRequest,
    OpenOrder,
    OrderRequest,
    OrderResult,
    Trade,
    is_bot_order,
)
from .transport import WebSocketFactory

# A minted read credential is fresh for 60 s and its socket lease ends 5 min
# after minting; the bot re-mints before either deadline.
READ_CREDENTIAL_MARGIN_SECONDS = 10
READ_SOCKET_ROTATE_SECONDS = 270


class OneApiError(RuntimeError):
    """An exchange failure with a sanitized, non-secret diagnostic."""


class OneApiHTTPError(OneApiError):
    """A refused HTTP status without response bodies, URLs or credential details."""

    def __init__(self, status_code: int, reason_code: str | None = None) -> None:
        self.status_code, self.reason_code = status_code, reason_code
        super().__init__(f"One API refused request (HTTP {status_code})")


class ActionRefused(OneApiError):
    """Glyde refused a whole signed action (``status: err``): none of its entries took effect."""


class EngineUnavailable(ActionRefused):
    """Glyde refused an exchange action because its engine is halted or not ready."""


class OutcomeUnknown(OneApiError):
    """A submitted action may have committed; reconcile, do not blindly re-sign/retry."""


class StreamDisconnected(OneApiError):
    """The socket closed or its transport failed; resubscribe from a fresh snapshot."""


class SubscriptionDeferred(StreamDisconnected):
    """The server refused a subscription for now; resubscribe no sooner than retry_after."""

    def __init__(self, reason_code: str, retry_after: float) -> None:
        self.reason_code, self.retry_after = reason_code, retry_after
        super().__init__(f"subscription deferred ({reason_code})")


# Glyde refuses an order, cancel or modify action outside 1..16 entries
# (docs/one-api/hl-compatibility.md); larger batches are split into several signed actions.
MAX_ACTION_ENTRIES = 16
RETRYABLE_REFUSALS = frozenset({"PROJECTION_UNAVAILABLE"})
REFUSAL_RETRY_MAX_SECONDS = 60.0
_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
# Refusal codes meaning Glyde's engine or its projection is halted or not ready yet.
ENGINE_UNAVAILABLE_REASONS = frozenset(
    {
        "ENGINE_NOT_READY",
        "ENGINE_HALTED",
        "ADAPTER_INTEGRITY_CONFLICT",
        "ADAPTER_INACTIVE",
        "PROJECTION_UNAVAILABLE",
        "MATCHING_PAUSED",
        "NATIVE_READ_UNAVAILABLE",
    }
)
_ENGINE_UNAVAILABLE_TEXT = "trading is unavailable"
_REFUSAL_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9 ,.:;'()/_-]{0,199}")
_RESUBSCRIBE_TEXT = {
    "Glyde: resnapshot required": "RESNAPSHOT_REQUIRED",
    "Glyde: projection unavailable": "PROJECTION_UNAVAILABLE",
}


def reason_code(value: object) -> str | None:
    return value if isinstance(value, str) and _REASON_CODE.fullmatch(value) else None


def engine_unavailable(error: BaseException | None) -> bool:
    """A refusal Glyde sends while its engine is halted or not ready; never a plain drop."""
    if isinstance(error, EngineUnavailable):
        return True
    if isinstance(error, SubscriptionDeferred):
        return error.reason_code in ENGINE_UNAVAILABLE_REASONS
    return isinstance(error, OneApiHTTPError) and error.reason_code in ENGINE_UNAVAILABLE_REASONS


def subscription_refusal(frame: dict[str, Any]) -> OneApiError:
    """Classify an error frame: retryAfter or a known transient reasonCode defers, else refuses."""
    reason = reason_code(frame.get("reasonCode")) or "UNSPECIFIED"
    hint = frame.get("retryAfter")
    if isinstance(hint, (int, float)) and not isinstance(hint, bool):
        retry_after = float(hint)
        if math.isfinite(retry_after) and retry_after >= 0:
            return SubscriptionDeferred(reason, min(retry_after, REFUSAL_RETRY_MAX_SECONDS))
    if reason in RETRYABLE_REFUSALS or reason in ENGINE_UNAVAILABLE_REASONS:
        return SubscriptionDeferred(reason, 0.0)
    if reason == "UNSPECIFIED" and frame.get("data") in _RESUBSCRIBE_TEXT:
        # Glyde's uncoded projection refusals: a fresh snapshot repairs both.
        return SubscriptionDeferred(_RESUBSCRIBE_TEXT[frame["data"]], 0.0)
    return OneApiError("WebSocket subscription refused")


def reconnectable(error: BaseException) -> bool:
    """Socket drops, deferred subscriptions and transient admission failures; never refusals."""
    if isinstance(error, OneApiHTTPError):
        return error.status_code == 429 or error.status_code >= 500
    return isinstance(error, (StreamDisconnected, OSError, TimeoutError, WebSocketException))


# Socket admission retries must finish inside the runner's 10s first-book window.
SOCKET_ADMISSION_SECONDS = 8.0
SOCKET_RETRY_MAX_DELAY = 4.0


def _retry_after_seconds(value: str | None) -> float:
    try:
        seconds = float(value or "")
    except ValueError:
        return 0.0
    return min(max(seconds, 0.0), SOCKET_RETRY_MAX_DELAY)


def wire_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError("price and quantity must be finite positive Decimals")
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def book_from_wire(data: dict[str, Any]) -> Book:
    levels = data["levels"]
    if len(levels) != 2:
        raise OneApiError("invalid book levels")
    sides = tuple(
        tuple(Level(decimal(row["px"]), decimal(row["sz"])) for row in side) for side in levels
    )
    for side in sides:
        if any(level.price <= 0 or level.quantity <= 0 for level in side):
            raise OneApiError("invalid book level")
    if (
        any(sides[0][i].price < sides[0][i + 1].price for i in range(len(sides[0]) - 1))
        or any(sides[1][i].price > sides[1][i + 1].price for i in range(len(sides[1]) - 1))
        or (sides[0] and sides[1] and sides[0][0].price >= sides[1][0].price)
    ):
        raise OneApiError("unordered or crossed book")
    return Book(str(data["coin"]), sides[0], sides[1], int(data["time"]))


def trades_from_wire(data: object) -> tuple[Trade, ...]:
    try:
        if not isinstance(data, list) or len(data) > 4096:
            raise ValueError
        result = []
        for row in data:
            tid = row["tid"]
            if (
                (type(tid) is not int and not isinstance(tid, str))
                or not str(tid).isascii()
                or not str(tid).isdigit()
                or row["side"] not in {"B", "A"}
            ):
                raise ValueError
            result.append(
                Trade(
                    row["coin"],
                    "buy" if row["side"] == "B" else "sell",
                    decimal(row["sz"]),
                    decimal(row["px"]),
                    row["time"],
                    str(tid),
                )
            )
        return tuple(result)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise OneApiError("invalid public trades") from None


def account_from_wire(state: dict[str, Any]) -> Account:
    try:
        unrealized = sum(
            (decimal(row["position"]["unrealizedPnl"]) for row in state["assetPositions"]),
            Decimal(0),
        )
    except (KeyError, ValueError, TypeError):
        unrealized = None
    return Account(
        decimal(state["marginSummary"]["accountValue"]),
        {
            row["position"]["coin"]: decimal(row["position"]["szi"])
            for row in state["assetPositions"]
        },
        decimal(state["withdrawable"]),
        unrealized_pnl=unrealized,
    )


def orders_from_wire(rows: list[dict[str, Any]]) -> list[OpenOrder]:
    result = []
    for row in rows:
        if row["side"] not in {"B", "A"}:
            raise OneApiError("unknown open-order side")
        result.append(
            OpenOrder(
                str(row["oid"]),
                row["coin"],
                "buy" if row["side"] == "B" else "sell",
                decimal(row["sz"]),
                decimal(row["limitPx"]),
                row.get("cloid"),
                row.get("reduceOnly", False),
            )
        )
    return result


def history_from_wire(rows: object) -> list[HistoricalOrder]:
    try:
        if not isinstance(rows, list):
            raise TypeError
        result = []
        for row in rows:
            order, status, cloid = row["order"], row["status"], row["order"].get("cloid")
            if (
                not isinstance(status, str)
                or not isinstance(order["coin"], str)
                or (cloid is not None and not isinstance(cloid, str))
            ):
                raise TypeError
            result.append(HistoricalOrder(str(order["oid"]), order["coin"], status, cloid))
        return result
    except (KeyError, TypeError, AttributeError):
        raise OneApiError("invalid historical orders") from None


class OneApiClient:
    def __init__(
        self,
        config: Config,
        *,
        http_client: httpx.AsyncClient | None = None,
        connect_ws: WebSocketFactory = websockets.connect,
        heartbeat_interval: float = 20,
    ) -> None:
        self.config = config
        self._http = http_client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self._owns_http = http_client is None
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat interval must be positive")
        self._heartbeat_interval = heartbeat_interval
        self._connect_ws = connect_ws
        self._wallet = None
        self._assets: dict[str, tuple[int, int]] = {}
        self._last_nonce = 0
        self._sign_lock = asyncio.Lock()
        self.public_clock: tuple[float, float, float] | None = None
        self._read_credential: tuple[str, float] | None = None
        self._read_lock = asyncio.Lock()

    async def __aenter__(self) -> OneApiClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _agent_wallet(self) -> Any:
        if self._wallet is None:
            try:
                self._wallet = EthAccount.from_key(self.config.secret(self.config.private_key_env))
            except (ValueError, TypeError):
                raise ValueError("invalid API wallet key environment configuration") from None
        return self._wallet

    def minted_reads(self) -> bool:
        """Without a configured read key, the agent key mints short-lived read credentials."""
        return not os.environ.get(self.config.read_api_key_env)

    async def read_key(self, *, fresh: bool = False) -> str:
        """The configured read key, or one the approved agent minted for its owner.

        A socket consumes its credential, so pass ``fresh`` for every connect.
        """
        if not self.minted_reads():
            return self.config.secret(self.config.read_api_key_env)
        async with self._read_lock:
            cached = self._read_credential
            if not fresh and cached is not None and time.time() < cached[1]:
                return cached[0]
            credential, expires_at = await self._mint_read_credential()
            self._read_credential = credential, expires_at - READ_CREDENTIAL_MARGIN_SECONDS
            return credential

    async def _mint_read_credential(self) -> tuple[str, float]:
        if not self.config.owner or not self.config.vault_address:
            raise ValueError("agent read sessions require the registered owner and vault_address")
        wallet = self._agent_wallet()
        message = {
            "owner": self.config.owner.lower(),
            "agentAddress": wallet.address.lower(),
            "issuedAt": time.time_ns() // 1_000_000,
            "nonce": secrets.randbits(63) or 1,
        }
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
            "domain": {
                "name": "Glyde",
                "version": "1",
                "chainId": 42161 if self.config.mainnet else 421614,
                "verifyingContract": self.config.vault_address.lower(),
            },
            "message": message,
        }
        signed = wallet.sign_message(encode_typed_data(full_message=typed))
        body = {
            **message,
            "signature": {"r": hex(signed.r), "s": hex(signed.s), "v": signed.v},
        }
        result = await self._post("/one-api/session/agent", body)
        try:
            data = result["data"]
            credential, expires_ms = data["readCredential"], data["expiresAt"]
            if not isinstance(credential, str) or not credential or type(expires_ms) is not int:
                raise TypeError
        except (KeyError, TypeError):
            raise OneApiError("agent read session returned an invalid credential") from None
        return credential, expires_ms / 1000

    async def _post(
        self, path: str, body: dict[str, Any], *, private: bool = False, action: bool = False
    ) -> Any:
        headers = {"X-Glyde-API-Key": await self.read_key()} if private else {}
        sent_at = time.time()
        try:
            response = await self._http.post(
                self.config.base_url.rstrip("/") + path, json=body, headers=headers
            )
        except httpx.TransportError:
            if action:
                raise OutcomeUnknown(
                    "exchange transport failed; reconcile the original request"
                ) from None
            raise OneApiError("One API transport failed") from None
        if not private and not action:
            self.public_clock = None
            try:
                date = parsedate_to_datetime(response.headers["Date"])
                if date.tzinfo is not None:
                    self.public_clock = sent_at, time.time(), date.timestamp()
            except (KeyError, ValueError, TypeError, OverflowError, OSError):
                pass
        if response.status_code >= 500 and action:
            raise OutcomeUnknown("exchange outcome unknown; reconcile the original request")
        if response.status_code == 503:
            try:
                code = reason_code(response.json().get("reasonCode"))
            except (ValueError, AttributeError):
                code = None
            raise OneApiHTTPError(503, code)
        if response.status_code != 200:
            raise OneApiHTTPError(response.status_code)
        try:
            result = response.json()
        except ValueError:
            if action:
                raise OutcomeUnknown("exchange returned an invalid receipt") from None
            raise OneApiError("One API returned invalid JSON") from None
        return result

    async def engine_ready(self) -> bool | None:
        """Glyde's public health: True when READY, False while it reports the engine
        unavailable (HTTP 503 with a state), None when health itself is unreachable."""
        try:
            response = await self._http.get(self.config.base_url.rstrip("/") + "/health")
            state = response.json().get("state")
        except (httpx.TransportError, ValueError, AttributeError):
            return None
        if response.status_code == 200 and state == "READY":
            return True
        if response.status_code == 503 and reason_code(state) is not None:
            return False
        return None

    async def info(self, body: dict[str, Any]) -> Any:
        return await self._post(
            "/info",
            body,
            private=body.get("type") in {"clearinghouseState", "openOrders", "historicalOrders"},
        )

    async def meta(self) -> dict[str, Any]:
        result = await self.info({"type": "meta"})
        self._assets = {
            asset["name"]: (index, int(asset["szDecimals"]))
            for index, asset in enumerate(result["universe"])
        }
        return cast(dict[str, Any], result)

    async def book(self, symbol: str) -> Book:
        return book_from_wire(await self.info({"type": "l2Book", "coin": symbol}))

    async def all_mids(self) -> dict[str, Decimal]:
        return {
            key: decimal(value) for key, value in (await self.info({"type": "allMids"})).items()
        }

    async def account(self) -> Account:
        return account_from_wire(
            await self.info({"type": "clearinghouseState", "user": self.config.owner})
        )

    async def open_orders(self) -> list[OpenOrder]:
        return orders_from_wire(await self.info({"type": "openOrders", "user": self.config.owner}))

    async def historical_orders(self) -> list[HistoricalOrder]:
        """The owner's recent orders with their latest status, newest first."""
        return history_from_wire(
            await self.info({"type": "historicalOrders", "user": self.config.owner})
        )

    async def _asset(self, symbol: str) -> tuple[int, int]:
        if not self._assets:
            await self.meta()
        if symbol not in self._assets:
            raise ValueError("symbol is absent from exchange metadata")
        return self._assets[symbol]

    async def _order_wire(self, request: OrderRequest) -> dict[str, Any]:
        asset, size_decimals = await self._asset(request.symbol)
        price, quantity = wire_decimal(request.price), wire_decimal(request.quantity)
        if request.side not in {"buy", "sell"} or request.time_in_force not in {
            "Alo",
            "Ioc",
            "Gtc",
        }:
            raise ValueError("invalid order side or time in force")
        if request.quantity != request.quantity.quantize(Decimal(1).scaleb(-size_decimals)):
            raise ValueError("quantity exceeds exchange size precision")
        wire = {
            "a": asset,
            "b": request.side == "buy",
            "p": price,
            "s": quantity,
            "r": request.reduce_only,
            "t": {"limit": {"tif": request.time_in_force}},
        }
        if request.client_order_id is not None:
            if not re.fullmatch(r"0x[0-9a-fA-F]{32}", request.client_order_id):
                raise ValueError("client_order_id must be a 16-byte hex cloid")
            wire["c"] = request.client_order_id
        return wire

    async def exchange(self, action: dict[str, Any]) -> dict[str, Any]:
        if self.config.mode != "live":
            raise OneApiError("paper mode cannot submit exchange actions")
        if not self.config.owner or not self.config.vault_address:
            raise ValueError("live mode requires the registered owner and vault_address namespace")
        # Only nonce allocation/signing is serialized; unrelated provider calls can proceed.
        async with self._sign_lock:
            self._agent_wallet()
            nonce = max(time.time_ns() // 1_000_000, self._last_nonce + 1)
            self._last_nonce = nonce
            # Glyde verifies L1 actions in realm/source "b" on every network, production included;
            # config.mainnet only selects the Glyde grant/read-session chain (42161 vs 421614).
            signature = sign_l1_action(
                self._wallet, action, self.config.vault_address, nonce, None, False
            )
            envelope = {
                "action": action,
                "nonce": nonce,
                "signature": signature,
                "vaultAddress": self.config.vault_address,
            }
        result = await self._post("/exchange", envelope, action=True)
        if not isinstance(result, dict) or result.get("status") not in {"ok", "err"}:
            raise OutcomeUnknown("exchange returned an unrecognized receipt")
        return result

    async def _chunked(
        self,
        rows: Sequence[Any],
        orders: Sequence[OrderRequest],
        action: Callable[[Sequence[Any]], Awaitable[dict[str, Any]]],
    ) -> list[OrderResult]:
        """Send at most MAX_ACTION_ENTRIES per signed action, in order.

        A refusal of the first action means nothing was placed and is raised. Once an earlier
        action has placed orders, a refused later action (and every one after it, which is
        not sent) is reported per order as rejected, so placed orders stay accounted for.
        """
        results: list[OrderResult] = []
        for start in range(0, len(rows), MAX_ACTION_ENTRIES):
            part = slice(start, start + MAX_ACTION_ENTRIES)
            try:
                results.extend(self._order_results(await action(rows[part]), orders[part]))
            except ActionRefused:
                if not start:
                    raise
                results.extend(
                    OrderResult(
                        "rejected", client_order_id=row.client_order_id, error="order refused"
                    )
                    for row in orders[start:]
                )
                break
        return results

    async def place_orders(self, orders: Sequence[OrderRequest]) -> list[OrderResult]:
        if not orders:
            return []
        wires = [await self._order_wire(order) for order in orders]
        return await self._chunked(
            wires,
            orders,
            lambda part: self.exchange({"type": "order", "orders": list(part), "grouping": "na"}),
        )

    @staticmethod
    def _statuses(result: dict[str, Any], count: int) -> list[Any]:
        if result.get("status") != "ok":
            refusal = result.get("response")
            if isinstance(refusal, str) and _ENGINE_UNAVAILABLE_TEXT in refusal.lower():
                raise EngineUnavailable("exchange refused the action: engine unavailable")
            # Glyde's refusal texts are short constants; anything else stays generic.
            reason = (
                f": {refusal}"
                if isinstance(refusal, str)
                and _REFUSAL_TEXT.fullmatch(refusal)
                and "0x" not in refusal
                else ""
            )
            raise ActionRefused(f"exchange refused the action{reason}")
        try:
            statuses = result["response"]["data"]["statuses"]
            if not isinstance(statuses, list) or len(statuses) != count:
                raise ValueError
            return statuses
        except (KeyError, TypeError, ValueError):
            raise OutcomeUnknown("exchange receipt does not match the submitted batch") from None

    @staticmethod
    def _receipt_order_id(value: object) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise OutcomeUnknown("invalid order receipt identity")
        text = str(value)
        if not re.fullmatch(r"[1-9][0-9]{0,19}", text) or int(text) > 2**64 - 1:
            raise OutcomeUnknown("invalid order receipt identity")
        return text

    def _order_results(
        self, result: dict[str, Any], orders: Sequence[OrderRequest]
    ) -> list[OrderResult]:
        responses = []
        for request, status in zip(orders, self._statuses(result, len(orders)), strict=True):
            if not isinstance(status, dict) or len(status) != 1:
                raise OutcomeUnknown("unrecognized or contradictory order receipt")
            if "error" in status and isinstance(status["error"], str):
                responses.append(
                    OrderResult(
                        "rejected", client_order_id=request.client_order_id, error="order refused"
                    )
                )
                continue
            kind = next(iter(status))
            if kind not in {"resting", "filled"} or not isinstance(status[kind], dict):
                raise OutcomeUnknown("unrecognized order receipt")
            row = status[kind]
            required = {"oid"} if kind == "resting" else {"oid", "totalSz", "avgPx"}
            if not required <= row.keys() or row.keys() - required - {"cloid"}:
                raise OutcomeUnknown("invalid order receipt shape")
            oid = self._receipt_order_id(row["oid"])
            if "cloid" in row and row["cloid"] != request.client_order_id:
                raise OutcomeUnknown("order receipt client identity mismatch")
            if kind == "filled":
                try:
                    if not isinstance(row["totalSz"], str) or not isinstance(row["avgPx"], str):
                        raise ValueError
                    size, price = decimal(row["totalSz"]), decimal(row["avgPx"])
                    if not 0 < size <= request.quantity or price <= 0:
                        raise ValueError
                    if (request.side == "buy" and price > request.price) or (
                        request.side == "sell" and price < request.price
                    ):
                        raise ValueError
                except ValueError:
                    raise OutcomeUnknown("invalid filled-order receipt economics") from None
            responses.append(OrderResult(kind, oid, request.client_order_id))
        return responses

    async def modify_orders(self, orders: Sequence[ModifyRequest]) -> list[OrderResult]:
        if not orders:
            return []
        modifies = [
            {"oid": int(row.order_id), "order": await self._order_wire(row.order)} for row in orders
        ]
        return await self._chunked(
            modifies,
            [row.order for row in orders],
            lambda part: self.exchange({"type": "batchModify", "modifies": list(part)}),
        )

    async def cancel_orders(self, orders: Sequence[CancelRequest]) -> list[CancelResult]:
        # Different cancel wire types require separate signed actions.
        results: list[CancelResult] = []
        for request in orders:
            if (request.order_id is None) == (request.client_order_id is None):
                raise ValueError("cancel requires exactly one order_id or client_order_id")
            asset, _ = await self._asset(request.symbol)
            if request.order_id is not None:
                action = {"type": "cancel", "cancels": [{"a": asset, "o": int(request.order_id)}]}
            else:
                if not re.fullmatch(r"0x[0-9a-fA-F]{32}", request.client_order_id or ""):
                    raise ValueError("invalid cancel client_order_id")
                action = {
                    "type": "cancelByCloid",
                    "cancels": [{"asset": asset, "cloid": request.client_order_id}],
                }
            status = self._statuses(await self.exchange(action), 1)[0]
            results.append(
                CancelResult(
                    "cancelled" if status == "success" else "rejected",
                    request.order_id,
                    request.client_order_id,
                    None if status == "success" else "cancel refused",
                )
            )
        return results

    async def cancel_all(self, symbol: str | None = None) -> None:
        """Cancel the bot's tagged orders; the account's other orders are left resting."""

        def owned(row: OpenOrder) -> bool:
            return is_bot_order(row.client_order_id) and symbol in (None, row.symbol)

        rows = await self.open_orders()
        results = await self.cancel_orders(
            [CancelRequest(row.symbol, row.order_id) for row in rows if owned(row)]
        )
        if any(result.status != "cancelled" for result in results):
            raise OneApiError("one or more cancel-all requests were refused")
        if any(owned(row) for row in await self.open_orders()):
            raise OneApiError("cancel-all could not confirm the bot's orders are gone")

    @asynccontextmanager
    async def _admitted_socket(self, url: str, **options: Any) -> AsyncIterator[Any]:
        """Open a socket, backing off on HTTP 429 admission refusals within a bounded window."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SOCKET_ADMISSION_SECONDS
        delay = 1.0
        while True:
            stack = AsyncExitStack()
            try:
                socket = await stack.enter_async_context(self._connect_ws(url, **options))
            except InvalidStatus as error:
                status = error.response.status_code
                remaining = deadline - loop.time()
                if status != 429 or remaining <= 0:
                    raise OneApiHTTPError(status) from None
                retry_after = _retry_after_seconds(error.response.headers.get("Retry-After"))
                await asyncio.sleep(min(max(retry_after, delay), remaining))
                delay = min(delay * 2, SOCKET_RETRY_MAX_DELAY)
                continue
            async with stack:
                yield socket
            return

    async def stream(
        self, subscriptions: Sequence[dict[str, Any]]
    ) -> AsyncGenerator[dict[str, Any], None]:
        parsed = urlsplit(self.config.base_url)
        url = urlunsplit(
            (
                "wss" if parsed.scheme == "https" else "ws",
                parsed.netloc,
                parsed.path.rstrip("/") + "/ws",
                "",
                "",
            )
        )
        private = any(
            row.get("type") in {"orderUpdates", "userFills", "clearinghouseState", "openOrders"}
            for row in subscriptions
        )
        headers = {"X-Glyde-API-Key": await self.read_key(fresh=True)} if private else {}
        rotate_at = (
            time.monotonic() + READ_SOCKET_ROTATE_SECONDS
            if private and self.minted_reads()
            else None
        )
        async with self._admitted_socket(
            url,
            additional_headers=headers,
            max_size=512 * 1024,
            max_queue=32,
            open_timeout=10,
            close_timeout=5,
        ) as socket:
            for subscription in subscriptions:
                await socket.send(json.dumps({"method": "subscribe", "subscription": subscription}))

            async def heartbeat() -> None:
                try:
                    while True:
                        await asyncio.sleep(self._heartbeat_interval)
                        await socket.send('{"method":"ping"}')
                except Exception:
                    try:
                        await socket.close()
                    finally:
                        raise OneApiError("WebSocket heartbeat failed") from None

            task = asyncio.create_task(heartbeat())
            try:
                async for raw in socket:
                    if not isinstance(raw, str):
                        raise OneApiError("unexpected binary WebSocket frame")
                    try:
                        frame = json.loads(raw)
                    except ValueError:
                        raise OneApiError("invalid WebSocket JSON") from None
                    if not isinstance(frame, dict):
                        raise OneApiError("WebSocket subscription refused")
                    if frame.get("channel") == "error":
                        raise subscription_refusal(frame)
                    yield frame
                    if rotate_at is not None and time.monotonic() >= rotate_at:
                        # Reconnect with a fresh credential before the server ends the lease.
                        raise StreamDisconnected("read credential rotation")
            except (ConnectionClosed, OSError):
                raise StreamDisconnected("WebSocket disconnected") from None
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
