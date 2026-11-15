"""Deterministic, in-process HL-shaped reads, orders and event streams.

This is a matching fixture, not a model of venue authentication, fees or margin.
There are no listeners, network calls, background tasks or wall-clock timers.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .websocket import FakeWebSocket

DEFAULT_USER = "0x" + "1" * 40
ZERO = Decimal(0)


def number(value: Any) -> Decimal:
    if isinstance(value, (float, bool)):
        raise ValueError("use exact decimal strings")
    try:
        result = Decimal(value)
    except (TypeError, ValueError, InvalidOperation) as error:
        raise ValueError("invalid decimal") from error
    if not result.is_finite():
        raise ValueError("non-finite decimal")
    return result


def text(value: Decimal) -> str:
    return format(value, "f")


@dataclass
class _Order:
    oid: int
    coin: str
    buy: bool
    price: Decimal
    remaining: Decimal
    original: Decimal
    user: str | None
    timestamp: int
    cloid: str | None = None
    reduce_only: bool = False

    def wire(self) -> dict[str, Any]:
        result = {
            "coin": self.coin,
            "side": "B" if self.buy else "A",
            "limitPx": text(self.price),
            "sz": text(self.remaining),
            "origSz": text(self.original),
            "reduceOnly": self.reduce_only,
            "oid": self.oid,
            "timestamp": self.timestamp,
        }
        if self.cloid is not None:
            result["cloid"] = self.cloid
        return result


class FakeOneApiServer:
    """Inject http_transport() and connect_ws into OneApiClient to stay offline.

    Each HTTP transport is bound to a test user; signatures are not authentication
    evidence. Fixture controls can act as a second user without inventing a signer.
    """

    def __init__(
        self,
        *,
        user: str = DEFAULT_USER,
        symbols: dict[str, int] | None = None,
        balance: str = "10000",
        timestamp_ms: int = 1_700_000_000_000,
    ) -> None:
        self.user = user.lower()
        self.symbols = dict({"BTC": 5, "ETH": 4} if symbols is None else symbols)
        if not self.symbols or any(type(v) is not int or v < 0 for v in self.symbols.values()):
            raise ValueError("symbols must specify nonnegative size decimals")
        self.now_ms = timestamp_ms
        self._balance = number(balance)
        self._cash: dict[str, Decimal] = defaultdict(lambda: self._balance)
        self._positions: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
        self._orders: dict[int, _Order] = {}
        self._fills: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._last: dict[str, Decimal] = {}
        self._next_oid = 1
        self._trade_id = 0
        self._connections: set[FakeWebSocket] = set()
        self.requests: list[dict[str, Any]] = []

    def advance_time(self, milliseconds: int) -> None:
        if type(milliseconds) is not int or milliseconds < 0:
            raise ValueError("time must advance by a nonnegative integer")
        self.now_ms += milliseconds

    def _coin(self, asset: Any) -> str:
        if type(asset) is not int or not 0 <= asset < len(self.symbols):
            raise ValueError("unknown asset")
        return list(self.symbols)[asset]

    def _book(self, coin: str, buy: bool) -> list[_Order]:
        return sorted(
            (o for o in self._orders.values() if o.coin == coin and o.buy == buy),
            key=lambda o: (-o.price if buy else o.price, o.oid),
        )

    def _l2(self, coin: str) -> dict[str, Any]:
        if coin not in self.symbols:
            raise ValueError("unknown coin")
        levels = []
        for buy in (True, False):
            grouped: dict[Decimal, tuple[Decimal, int]] = {}
            for order in self._book(coin, buy):
                size, count = grouped.get(order.price, (ZERO, 0))
                grouped[order.price] = (size + order.remaining, count + 1)
            levels.append(
                [
                    {"px": text(price), "sz": text(size), "n": count}
                    for price, (size, count) in grouped.items()
                ]
            )
        return {"coin": coin, "time": self.now_ms, "levels": levels}

    def _mid(self, coin: str) -> Decimal | None:
        bids, asks = self._book(coin, True), self._book(coin, False)
        if bids and asks:
            return (bids[0].price + asks[0].price) / 2
        return self._last.get(coin)

    def info(self, body: dict[str, Any]) -> Any:
        kind = body.get("type")
        if kind == "meta":
            return {
                "universe": [
                    {"name": coin, "szDecimals": decimals, "maxLeverage": 1}
                    for coin, decimals in self.symbols.items()
                ]
            }
        if kind == "l2Book":
            return self._l2(body["coin"])
        if kind == "allMids":
            return {coin: text(mid) for coin in self.symbols if (mid := self._mid(coin))}
        user = str(body.get("user", self.user)).lower()
        if kind == "openOrders":
            return [o.wire() for o in self._orders.values() if o.user == user]
        if kind == "userFills":
            return deepcopy(self._fills[user])
        if kind == "clearinghouseState":
            unrealized, notional, positions = ZERO, ZERO, []
            for (owner, coin), (quantity, entry) in self._positions.items():
                if owner != user or not quantity:
                    continue
                mark = self._mid(coin) or entry
                pnl = quantity * (mark - entry)
                value = abs(quantity * mark)
                unrealized += pnl
                notional += value
                positions.append(
                    {
                        "type": "oneWay",
                        "position": {
                            "coin": coin,
                            "szi": text(quantity),
                            "entryPx": text(entry),
                            "positionValue": text(value),
                            "unrealizedPnl": text(pnl),
                            "returnOnEquity": text(pnl / value if value else ZERO),
                            "marginUsed": text(value),
                            "leverage": {"type": "cross", "value": 1},
                            "liquidationPx": None,
                            "cumFunding": {
                                "allTime": "0",
                                "sinceOpen": "0",
                                "sinceChange": "0",
                            },
                        },
                    }
                )
            summary = {
                "accountValue": text(self._cash[user] + unrealized),
                "totalNtlPos": text(notional),
                "totalRawUsd": text(self._cash[user]),
                "totalMarginUsed": text(notional),
            }
            return {
                "marginSummary": summary,
                "crossMarginSummary": dict(summary),
                "crossMaintenanceMarginUsed": "0",
                "assetPositions": positions,
                "withdrawable": text(max(ZERO, self._cash[user] + unrealized - notional)),
                "time": self.now_ms,
            }
        raise ValueError(f"unsupported info type: {kind}")

    def _new_order(self, wire: dict[str, Any], user: str | None) -> tuple[_Order, str]:
        coin = self._coin(wire.get("a"))
        price, size = number(wire.get("p")), number(wire.get("s"))
        if price <= 0 or size <= 0 or size != size.quantize(Decimal(1).scaleb(-self.symbols[coin])):
            raise ValueError("invalid price or size precision")
        if type(wire.get("b")) is not bool or type(wire.get("r", False)) is not bool:
            raise ValueError("side and reduce-only must be booleans")
        order_type = wire.get("t", {})
        if not isinstance(order_type, dict) or not isinstance(order_type.get("limit"), dict):
            raise ValueError("limit order type required")
        tif = order_type["limit"].get("tif")
        if tif not in {"Gtc", "Alo", "Ioc"}:
            raise ValueError("only Gtc, Alo and Ioc limit orders are supported")
        cloid = wire.get("c")
        if cloid is not None:
            if not isinstance(cloid, str) or not re.fullmatch(r"0x[0-9a-fA-F]{32}", cloid):
                raise ValueError("cloid must be a 128-bit hex string")
            cloid = cloid.lower()
            if any(o.user == user and o.cloid == cloid for o in self._orders.values()):
                raise ValueError("duplicate open cloid")
        return _Order(
            0,
            coin,
            wire["b"],
            price,
            size,
            size,
            user,
            self.now_ms,
            cloid,
            wire.get("r", False),
        ), tif

    def _reduction(self, order: _Order) -> Decimal:
        quantity, _ = (
            self._positions.get((order.user, order.coin), (ZERO, ZERO))
            if order.user is not None
            else (ZERO, ZERO)
        )
        if not order.reduce_only:
            return order.remaining
        if not quantity or (quantity > 0) == order.buy:
            return ZERO
        return min(order.remaining, abs(quantity))

    def _update(self, order: _Order, status: str) -> None:
        if order.user is not None:
            self._broadcast(
                "orderUpdates",
                [
                    {
                        "order": order.wire(),
                        "status": status,
                        "statusTimestamp": self.now_ms,
                    }
                ],
                user=order.user,
            )

    def _position_fill(
        self, order: _Order, size: Decimal, price: Decimal, tid: int, crossed: bool
    ) -> None:
        if order.user is None:
            return
        key = (order.user, order.coin)
        old, entry = self._positions.get(key, (ZERO, ZERO))
        delta = size if order.buy else -size
        new, pnl = old + delta, ZERO
        if not old or (old > 0) == (delta > 0):
            entry = (abs(old) * entry + size * price) / abs(new)
        else:
            pnl = (price - entry) * min(abs(old), size) * (1 if old > 0 else -1)
            if not new:
                entry = ZERO
            elif (new > 0) != (old > 0):
                entry = price
        self._positions[key] = new, entry
        self._cash[order.user] += pnl
        fill = {
            "coin": order.coin,
            "px": text(price),
            "sz": text(size),
            "side": "B" if order.buy else "A",
            "time": self.now_ms,
            "startPosition": text(old),
            "closedPnl": text(pnl),
            "dir": ("Open Long" if order.buy else "Open Short")
            if not old or (old > 0) == (delta > 0)
            else ("Close Short" if order.buy else "Close Long"),
            "hash": "0x" + format(tid, "064x"),
            "oid": order.oid,
            "tid": tid,
            "crossed": crossed,
            "fee": "0",
            "feeToken": "USDC",
        }
        if order.cloid is not None:
            fill["cloid"] = order.cloid
        self._fills[order.user].append(fill)
        self._broadcast(
            "userFills",
            {"isSnapshot": False, "user": order.user, "fills": [fill]},
            user=order.user,
        )

    def _place(self, order: _Order, tif: str) -> dict[str, Any]:
        opposite = self._book(order.coin, not order.buy)
        crosses = opposite and (
            order.price >= opposite[0].price if order.buy else order.price <= opposite[0].price
        )
        if tif == "Alo" and crosses:
            return {"error": "Post only order would have immediately matched"}
        order.remaining = min(order.remaining, self._reduction(order))
        if not order.remaining:
            return {"error": "Reduce only order would increase position"}
        order.oid, self._next_oid = self._next_oid, self._next_oid + 1
        executed, total = ZERO, ZERO
        for maker in opposite:
            if (
                not order.remaining
                or (order.buy and order.price < maker.price)
                or (not order.buy and order.price > maker.price)
            ):
                break
            amount = min(
                order.remaining,
                maker.remaining,
                self._reduction(maker),
                self._reduction(order),
            )
            if not amount:
                del self._orders[maker.oid]
                self._update(maker, "canceled")
                continue
            if maker.user is not None and maker.user == order.user:
                del self._orders[maker.oid]
                self._update(maker, "canceled")
                continue
            maker.remaining -= amount
            order.remaining -= amount
            executed += amount
            total += amount * maker.price
            self._trade_id += 1
            self._last[order.coin] = maker.price
            self._position_fill(maker, amount, maker.price, self._trade_id, False)
            self._position_fill(order, amount, maker.price, self._trade_id, True)
            self._broadcast(
                "trades",
                [
                    {
                        "coin": order.coin,
                        "side": "B" if order.buy else "A",
                        "px": text(maker.price),
                        "sz": text(amount),
                        "time": self.now_ms,
                        "tid": self._trade_id,
                        "hash": "0x" + format(self._trade_id, "064x"),
                    }
                ],
                coin=order.coin,
            )
            if not maker.remaining:
                del self._orders[maker.oid]
            self._update(maker, "open" if maker.remaining else "filled")
        if order.remaining and tif != "Ioc":
            self._orders[order.oid] = order
            self._update(order, "open")
            return {"resting": {"oid": order.oid}}
        self._update(order, "filled" if executed else "canceled")
        if executed:
            return {
                "filled": {
                    "oid": order.oid,
                    "totalSz": text(executed),
                    "avgPx": text(total / executed),
                }
            }
        return {"error": "Order could not immediately match"}

    def _cancel(self, user: str, oid: Any, coin: str | None = None) -> Any:
        if isinstance(oid, str) and re.fullmatch(r"0x[0-9a-fA-F]{32}", oid):
            oid = oid.lower()
        elif type(oid) is not int or oid <= 0:
            return {"error": "A positive order ID or valid cloid is required"}
        order = next(
            (
                o
                for o in self._orders.values()
                if o.user == user
                and (o.oid == oid or o.cloid == oid)
                and (coin is None or o.coin == coin)
            ),
            None,
        )
        if order is None:
            return {"error": "Order was never placed, already canceled, or filled"}
        del self._orders[order.oid]
        self._update(order, "canceled")
        return "success"

    def exchange(self, envelope: dict[str, Any], *, user: str | None = None) -> dict[str, Any]:
        user = (user or self.user).lower()
        action = envelope.get("action", {})
        if not isinstance(action, dict):
            raise ValueError("action must be a JSON object")
        kind = action.get("type")
        if kind not in {"order", "cancel", "cancelByCloid", "batchModify", "modify"}:
            raise ValueError(f"unsupported exchange action: {kind}")
        statuses = []
        items = action.get("orders", []) if kind == "order" else action.get("cancels", [])
        if kind in {"modify", "batchModify"}:
            items = [action] if kind == "modify" else action.get("modifies", [])
        if not isinstance(items, list):
            raise ValueError("orders, cancels or modifies must be an array")
        for item in items:
            try:
                if not isinstance(item, dict):
                    raise ValueError("order must be a JSON object")
                if kind == "order":
                    order, tif = self._new_order(item, user)
                    statuses.append(self._place(order, tif))
                elif kind in {"cancel", "cancelByCloid"}:
                    asset = item.get("a") if kind == "cancel" else item.get("asset")
                    oid = item.get("o") if kind == "cancel" else item.get("cloid")
                    statuses.append(self._cancel(user, oid, self._coin(asset)))
                else:
                    old = next(
                        (
                            o
                            for o in self._orders.values()
                            if o.user == user and (o.oid == item["oid"] or o.cloid == item["oid"])
                        ),
                        None,
                    )
                    if old is None:
                        statuses.append({"error": "Order not found"})
                        continue
                    # A modify may retain its own client ID, but never another order's.
                    del self._orders[old.oid]
                    try:
                        order, tif = self._new_order(item["order"], user)
                    except (KeyError, TypeError, ValueError, InvalidOperation):
                        self._orders[old.oid] = old
                        raise
                    opposite = self._book(order.coin, not order.buy)
                    crosses = opposite and (
                        order.price >= opposite[0].price
                        if order.buy
                        else order.price <= opposite[0].price
                    )
                    if (tif == "Alo" and crosses) or not self._reduction(order):
                        self._orders[old.oid] = old
                        statuses.append({"error": "Replacement would violate order constraints"})
                        continue
                    self._update(old, "canceled")
                    statuses.append(self._place(order, tif))
            except (KeyError, TypeError, ValueError, InvalidOperation) as error:
                statuses.append({"error": str(error)})
        self._publish_books()
        result_type = "cancel" if kind in {"cancel", "cancelByCloid"} else "order"
        return {
            "status": "ok",
            "response": {"type": result_type, "data": {"statuses": statuses}},
        }

    def set_book(
        self, coin: str, *, bids: list[tuple[str, str]], asks: list[tuple[str, str]]
    ) -> None:
        """Replace external liquidity; new crossing levels fill resting user quotes."""
        asset = list(self.symbols).index(coin)
        orders = []
        for buy, levels in ((True, bids), (False, asks)):
            for price, size in levels:
                order, tif = self._new_order(
                    {
                        "a": asset,
                        "b": buy,
                        "p": price,
                        "s": size,
                        "t": {"limit": {"tif": "Gtc"}},
                    },
                    None,
                )
                orders.append((order, tif))
        if bids and asks and max(number(p) for p, _ in bids) >= min(number(p) for p, _ in asks):
            raise ValueError("external book must not be locked or crossed")
        self._orders = {
            oid: o for oid, o in self._orders.items() if o.coin != coin or o.user is not None
        }
        for order, tif in orders:
            self._place(order, tif)
        self._publish_books()

    def market_order(
        self, coin: str, *, buy: bool, size: str, user: str | None = None
    ) -> dict[str, Any]:
        """Explicit simulated counterparty trade; never contacts an exchange."""
        if user is None:
            user = "0x" + ("2" if self.user != "0x" + "2" * 40 else "1") * 40
        levels = self._book(coin, not buy)
        if not levels:
            return {"error": "No liquidity"}
        limit = max(o.price for o in levels) if buy else min(o.price for o in levels)
        return self.exchange(
            {
                "action": {
                    "type": "order",
                    "orders": [
                        {
                            "a": list(self.symbols).index(coin),
                            "b": buy,
                            "p": text(limit),
                            "s": size,
                            "t": {"limit": {"tif": "Ioc"}},
                        }
                    ],
                }
            },
            user=user,
        )

    def _validate_subscription(self, subscription: dict[str, Any]) -> None:
        kind = subscription.get("type")
        if kind in {"l2Book", "trades"}:
            if subscription.get("coin") not in self.symbols:
                raise ValueError("unknown coin")
        elif kind not in {
            "allMids",
            "orderUpdates",
            "userFills",
            "clearinghouseState",
            "openOrders",
        }:
            raise ValueError("unsupported subscription")
        if kind in {"orderUpdates", "userFills", "clearinghouseState", "openOrders"}:
            if not subscription.get("user"):
                raise ValueError("private subscription requires a test user")

    def _snapshot(self, ws: FakeWebSocket, subscription: dict[str, Any]) -> None:
        kind = subscription["type"]
        if kind == "orderUpdates":
            return
        data: Any
        user = str(subscription.get("user", self.user)).lower()
        if kind == "userFills":
            data = {
                "user": user,
                "isSnapshot": True,
                "fills": deepcopy(self._fills[user]),
            }
        elif kind == "trades":
            data = []
        elif kind == "allMids":
            data = {"mids": self.info({"type": "allMids"})}
        else:
            data = self.info(subscription)
            if kind == "clearinghouseState":
                data = {"user": user, "clearinghouseState": data}
            elif kind == "openOrders":
                data = {"user": user, "orders": data}
        ws.push({"channel": kind, "data": data})

    def _broadcast(
        self,
        channel: str,
        data: Any,
        *,
        coin: str | None = None,
        user: str | None = None,
    ) -> None:
        for ws in tuple(self._connections):
            for sub in ws.subscriptions:
                if (
                    sub["type"] == channel
                    and (coin is None or sub.get("coin") == coin)
                    and (user is None or str(sub.get("user", "")).lower() == user)
                ):
                    ws.push({"channel": channel, "data": data})
                    break

    def _publish_books(self) -> None:
        for coin in self.symbols:
            self._broadcast("l2Book", self._l2(coin), coin=coin)
        self._broadcast("allMids", {"mids": self.info({"type": "allMids"})})
        users = set(self._cash) | {o.user for o in self._orders.values() if o.user is not None}
        users.update(
            str(sub["user"]).lower()
            for ws in self._connections
            for sub in ws.subscriptions
            if "user" in sub
        )
        for user in users:
            self._broadcast(
                "clearinghouseState",
                {
                    "user": user,
                    "clearinghouseState": self.info({"type": "clearinghouseState", "user": user}),
                },
                user=user,
            )
            self._broadcast(
                "openOrders",
                {
                    "user": user,
                    "orders": self.info({"type": "openOrders", "user": user}),
                },
                user=user,
            )

    def connect_ws(self, url: str = "", **_: Any) -> FakeWebSocket:
        del url
        return FakeWebSocket(self)

    async def disconnect(self) -> None:
        """Drop every active fake connection; reconnects obtain fresh snapshots."""
        for ws in tuple(self._connections):
            await ws.close()

    def http_transport(self, *, user: str | None = None) -> Any:
        """httpx.MockTransport for AsyncClient; supports root and /v1 One API paths."""
        import httpx

        def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method != "POST" or path not in {
                "/info",
                "/exchange",
                "/v1/info",
                "/v1/exchange",
            }:
                return httpx.Response(404, json={"error": "unsupported fake endpoint"})
            try:
                body = json.loads(request.content)
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                self.requests.append({"path": path, "body": deepcopy(body)})
                result = (
                    self.info(body) if path.endswith("/info") else self.exchange(body, user=user)
                )
                return httpx.Response(200, json=result)
            except (KeyError, TypeError, ValueError) as error:
                return httpx.Response(422, json={"error": str(error)})

        return httpx.MockTransport(handle)
