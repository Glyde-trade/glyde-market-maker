"""Async One API transport using the official Hyperliquid SDK signing primitive."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import suppress
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from eth_account import Account as EthAccount
from hyperliquid.utils.signing import sign_l1_action

from .config import Config, decimal
from .models import (
    Account,
    Book,
    CancelRequest,
    CancelResult,
    Level,
    ModifyRequest,
    OpenOrder,
    OrderRequest,
    OrderResult,
    Trade,
)
from .transport import WebSocketFactory


class OneApiError(RuntimeError):
    """An exchange failure with a sanitized, non-secret diagnostic."""


class OneApiHTTPError(OneApiError):
    """A refused HTTP status without response bodies, URLs or credential details."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"One API refused request (HTTP {status_code})")


class OutcomeUnknown(OneApiError):
    """A submitted action may have committed; reconcile, do not blindly re-sign/retry."""


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
    return Account(
        decimal(state["marginSummary"]["accountValue"]),
        {
            row["position"]["coin"]: decimal(row["position"]["szi"])
            for row in state["assetPositions"]
        },
        decimal(state["withdrawable"]),
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

    async def __aenter__(self) -> OneApiClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _post(
        self, path: str, body: dict[str, Any], *, private: bool = False, action: bool = False
    ) -> Any:
        headers = (
            {"X-Glyde-API-Key": self.config.secret(self.config.read_api_key_env)} if private else {}
        )
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
        if response.status_code != 200:
            raise OneApiHTTPError(response.status_code)
        try:
            result = response.json()
        except ValueError:
            if action:
                raise OutcomeUnknown("exchange returned an invalid receipt") from None
            raise OneApiError("One API returned invalid JSON") from None
        return result

    async def info(self, body: dict[str, Any]) -> Any:
        return await self._post(
            "/info", body, private=body.get("type") in {"clearinghouseState", "openOrders"}
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
            if self._wallet is None:
                try:
                    self._wallet = EthAccount.from_key(
                        self.config.secret(self.config.private_key_env)
                    )
                except (ValueError, TypeError):
                    raise ValueError("invalid API wallet key environment configuration") from None
            nonce = max(time.time_ns() // 1_000_000, self._last_nonce + 1)
            self._last_nonce = nonce
            signature = sign_l1_action(
                self._wallet, action, self.config.vault_address, nonce, None, self.config.mainnet
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

    async def place_orders(self, orders: Sequence[OrderRequest]) -> list[OrderResult]:
        if not orders:
            return []
        action = {
            "type": "order",
            "orders": [await self._order_wire(order) for order in orders],
            "grouping": "na",
        }
        return self._order_results(await self.exchange(action), orders)

    @staticmethod
    def _statuses(result: dict[str, Any], count: int) -> list[Any]:
        if result.get("status") != "ok":
            raise OneApiError("exchange refused the action")
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
        action = {
            "type": "batchModify",
            "modifies": [
                {"oid": int(row.order_id), "order": await self._order_wire(row.order)}
                for row in orders
            ],
        }
        return self._order_results(await self.exchange(action), [row.order for row in orders])

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
        rows = await self.open_orders()
        results = await self.cancel_orders(
            [
                CancelRequest(row.symbol, row.order_id)
                for row in rows
                if symbol is None or row.symbol == symbol
            ]
        )
        if any(result.status != "cancelled" for result in results):
            raise OneApiError("one or more cancel-all requests were refused")
        remaining = await self.open_orders()
        if any(symbol is None or row.symbol == symbol for row in remaining):
            raise OneApiError("cancel-all could not confirm an empty open-order snapshot")

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
        headers = (
            {"X-Glyde-API-Key": self.config.secret(self.config.read_api_key_env)} if private else {}
        )
        async with self._connect_ws(
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
                    if not isinstance(frame, dict) or frame.get("channel") == "error":
                        raise OneApiError("WebSocket subscription refused")
                    yield frame
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
