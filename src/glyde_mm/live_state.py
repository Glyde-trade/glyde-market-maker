"""Sequenced live snapshots replace recurring HTTP reads; gaps latch until restart."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .metrics import Metrics

from .client import (
    OneApiError,
    StreamDisconnected,
    account_from_wire,
    book_from_wire,
    orders_from_wire,
    reconnectable,
)
from .config import Config, decimal
from .models import Account, CancelRequest, CancelResult, Fill, OpenOrder, OrderRequest, OrderResult
from .transport import JsonObject, MarketClient

OWNER_CHANNELS = ("clearinghouseState", "openOrders", "orderUpdates", "userFills")


def counter(value: object) -> int:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or str(int(value)) != value
        or len(value) > 20
        or int(value) > 2**64 - 1
    ):
        raise OneApiError("invalid stream counter")
    return int(value)


class LiveStateClient:
    def __init__(
        self, source: MarketClient, config: Config, *, metrics: Metrics | None = None
    ) -> None:
        self.source, self.config = source, config
        self.metrics = metrics
        self._account: Account | None = None
        self._account_time = 0
        self._orders: list[OpenOrder] = []
        self._groups: dict[str, JsonObject] = {}
        self._pending: dict[str, tuple[JsonObject, list[JsonObject], float]] = {}
        self._failure: OneApiError | None = None
        self._events: asyncio.Queue[JsonObject | None] = asyncio.Queue(32)
        self._changed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._seen_fills: dict[tuple[str, str], JsonObject] = {}
        self._terminal: set[str] = set()
        self._cancelled: dict[str, str] = {}
        self._awaiting: dict[str, OrderResult] = {}
        self._known: dict[str, OrderRequest] = {}

    def subscriptions(self) -> list[JsonObject]:
        subscriptions = [
            {
                "type": "l2Book",
                "coin": symbol,
                "glyde": {
                    "version": 1,
                    "delivery": "sequenced",
                    "group": f"mm-book-{i}",
                },
            }
            for i, symbol in enumerate(self.config.symbols)
        ]
        subscriptions.extend(
            {
                "type": channel,
                "user": self.config.owner,
                "glyde": {
                    "version": 1,
                    "delivery": "sequenced",
                    "group": "mm-owner",
                },
            }
            for channel in OWNER_CHANNELS
        )
        return subscriptions

    def invalidate(self, reason: str, *, disconnected: bool = False) -> None:
        failure = StreamDisconnected(reason) if disconnected else OneApiError(reason)
        self._failure = self._failure or failure
        self._changed.set()
        if not self._events.full():
            self._events.put_nowait(None)

    def _emit(self, frame: JsonObject) -> None:
        if self._events.full():
            raise OneApiError("live stream consumer overflow")
        self._events.put_nowait(frame)

    def check(self) -> None:
        if self._failure:
            raise self._failure
        now = time.monotonic()
        if any(now - started > 2 for _, _, started in self._pending.values()):
            self.invalidate("incomplete live stream batch")
            raise OneApiError("incomplete live stream batch")
        if self._account is not None and (
            not 0 <= now - self._account.received_at <= float(self.config.risk.account_age_seconds)
            or not -1
            <= time.time() - self._account_time / 1000
            <= float(self.config.risk.account_age_seconds)
        ):
            self.invalidate("stale live account stream")
            raise OneApiError("stale live account stream")

    async def _current(self) -> None:
        async with asyncio.timeout(2):
            while True:
                self.check()
                if (
                    self._account is not None
                    and "mm-owner" not in self._pending
                    and not any(order.order_id in self._cancelled for order in self._orders)
                ):
                    if all(
                        oid in self._terminal
                        or oid in self._cancelled
                        or (
                            result.status == "resting"
                            and any(
                                order.order_id == oid
                                and order.client_order_id == self._known[oid].client_order_id
                                for order in self._orders
                            )
                        )
                        for oid, result in self._awaiting.items()
                    ):
                        self._awaiting.clear()
                        return
                self._changed.clear()
                await self._changed.wait()

    async def account(self) -> Account:
        await self._current()
        assert self._account is not None
        return replace(self._account, positions=dict(self._account.positions))

    async def open_orders(self) -> list[OpenOrder]:
        await self._current()
        return list(self._orders)

    async def place_orders(self, orders: Sequence[OrderRequest]) -> list[OrderResult]:
        await self._current()
        results = await self.source.place_orders(orders)
        if len(results) != len(orders):
            self.invalidate("live action receipt mismatch")
            raise OneApiError("live action receipt mismatch")
        for order, result in zip(orders, results, strict=True):
            if result.status in {"resting", "filled"} and result.order_id:
                self._known[result.order_id] = order
                self._awaiting[result.order_id] = result
                if len(self._known) > 8192:
                    self.invalidate("live order evidence budget exceeded")
                    raise OneApiError("live order evidence budget exceeded")
        return results

    async def cancel_orders(self, requests: Sequence[CancelRequest]) -> list[CancelResult]:
        results = []
        for request in requests:
            oid = request.order_id or next(
                (
                    oid
                    for oid, order in self._known.items()
                    if order.client_order_id == request.client_order_id
                    and order.symbol == request.symbol
                ),
                None,
            )
            if oid in self._cancelled:
                if self._cancelled[oid] != request.symbol:
                    raise OneApiError("cancel symbol mismatch")
                results.append(
                    CancelResult("already_closed", request.order_id, request.client_order_id)
                )
            elif oid in self._terminal and oid in self._known:
                if self._known[oid].symbol != request.symbol:
                    raise OneApiError("cancel symbol mismatch")
                results.append(
                    CancelResult("already_closed", request.order_id, request.client_order_id)
                )
            else:
                reply = await self.source.cancel_orders([request])
                if len(reply) != 1:
                    raise OneApiError("live cancel receipt mismatch")
                result = reply[0]
                if result.order_id not in (
                    None,
                    request.order_id,
                ) or result.client_order_id not in (None, request.client_order_id):
                    raise OneApiError("live cancel receipt identity mismatch")
                if oid is not None and result.status in {"cancelled", "already_closed"}:
                    self._cancelled[oid] = request.symbol
                results.extend(reply)
        return results

    async def cancel_all(self, symbol: str | None = None) -> None:
        # Retain original IDs for cleanup even after read authority or continuity is lost.
        known = {row.order_id: row.symbol for row in self._orders}
        known.update({oid: order.symbol for oid, order in self._known.items()})
        requests = [
            CancelRequest(coin, oid)
            for oid, coin in known.items()
            if (symbol is None or coin == symbol) and oid not in self._terminal
        ]
        results = await self.cancel_orders(requests)
        if any(row.status not in {"cancelled", "already_closed"} for row in results):
            raise OneApiError("live cleanup cancellation unconfirmed")
        if self._failure is not None:
            # Every retained ID has a confirmed terminal/cancel receipt. A dead read stream
            # cannot confirm a new snapshot, and remains latched against reads or new orders.
            return
        await self._current()
        async with asyncio.timeout(2):
            while any(symbol is None or row.symbol == symbol for row in self._orders):
                self.check()
                self._changed.clear()
                await self._changed.wait()

    async def stream(self) -> AsyncGenerator[JsonObject, None]:
        if self._task is not None:
            raise OneApiError("live stream cannot resume without a new snapshot")
        self._task = asyncio.create_task(self._receive())
        while True:
            frame = await self._events.get()
            self.check()
            if frame is None:
                raise OneApiError("live stream disconnected")
            yield frame

    async def close(self) -> None:
        self.invalidate("live stream closed")
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _receive(self) -> None:
        stream = self.source.stream(self.subscriptions())
        try:
            async for frame in stream:
                self.consume(frame)
            self.invalidate("live stream disconnected", disconnected=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # consume() latches protocol failures first; only a transport drop may reconnect.
            if reconnectable(error):
                self.invalidate("live stream disconnected", disconnected=True)
            else:
                self.invalidate("live stream invalid, gapped or disconnected")
        finally:
            await stream.aclose()

    def consume(self, frame: JsonObject) -> None:
        try:
            self._consume(frame)
        except Exception as error:
            if (
                self.metrics
                and self._failure is None
                and str(error) == "live stream continuity gap"
            ):
                self.metrics.count("gaps")
            self.invalidate("live stream invalid, gapped or disconnected")
            raise

    def _consume(self, frame: JsonObject) -> None:
        self.check()
        channel = frame.get("channel")
        if channel in {"pong", "subscriptionResponse"}:
            return
        if channel in {"error", "glydeSafety", "resnapshot_required"}:
            raise OneApiError("live stream requires resnapshot")
        meta = frame["glyde"]
        group = meta["group"]
        owner_group = group == "mm-owner"
        expected = set(OWNER_CHANNELS) if owner_group else {"l2Book"}
        if not owner_group and group not in {
            f"mm-book-{i}" for i in range(len(self.config.symbols))
        }:
            raise OneApiError("unexpected stream group")
        if (
            type(meta["version"]) is not int
            or meta["version"] != 1
            or meta["source"] != ("engine" if owner_group else "market")
            or not all(
                isinstance(meta[k], str) and 0 < len(meta[k]) <= 128 for k in ("session", "epoch")
            )
            or (owner_group and meta.get("owner", "").lower() != self.config.owner.lower())
        ):
            raise OneApiError("stream binding mismatch")
        publication = meta.get("nativePublication")
        if publication is not None and (
            not isinstance(publication, dict)
            or publication.get("unsafeState") is not False
            or publication.get("authorityAvailable") is not True
        ):
            raise OneApiError("unsafe live publication")
        batch = meta["batch"]
        declarations = batch["parts"]
        if (
            not isinstance(declarations, list)
            or not 1 <= len(declarations) <= 4
            or batch["kind"] not in {"snapshot", "delta"}
            or not isinstance(batch["id"], str)
            or not 0 < len(batch["id"]) <= 256
            or any(
                type(d["index"]) is not int
                or d["index"] != i
                or not isinstance(d["subscriptionId"], str)
                for i, d in enumerate(declarations)
            )
            or len({d["subscriptionId"] for d in declarations}) != len(declarations)
            or len({d["channel"] for d in declarations}) != len(declarations)
            or not {d["channel"] for d in declarations} <= expected
        ):
            raise OneApiError("invalid live batch membership")
        seq = counter(meta["sequence"])
        if seq == 0:
            raise OneApiError("invalid initial stream sequence")
        cursor = meta["cursor"]
        if set(cursor) != {"command", "transaction"}:
            raise OneApiError("mixed stream cursor domain")
        cut = (counter(cursor["command"]), counter(cursor["transaction"]))
        previous = self._groups.get(group)
        installed = self._account is not None if owner_group else previous is not None
        if previous is not None and installed:
            if (
                meta["session"] != previous["session"]
                or meta["epoch"] != previous["epoch"]
                or seq != counter(previous["sequence"]) + 1
                or any(
                    cut[i] < counter(previous["cursor"][k])
                    for i, k in enumerate(("command", "transaction"))
                )
            ):
                raise OneApiError("live stream continuity gap")
        if (
            (
                owner_group
                and not installed
                and {d["channel"] for d in declarations} == expected
                and batch["kind"] != "snapshot"
            )
            or (batch["kind"] == "snapshot" and meta["previous"] is not None)
            or (
                batch["kind"] == "delta"
                and (previous is None or counter(meta["previous"]) != counter(previous["sequence"]))
            )
            or (previous is None and batch["kind"] != "snapshot")
            or (installed and {d["channel"] for d in declarations} != expected)
        ):
            raise OneApiError("live stream baseline or predecessor missing")
        comparable = {**meta, "batch": {k: v for k, v in batch.items() if k != "index"}}
        pending = self._pending.get(group)
        if pending is None:
            if channel == "glydeBatchEnd" or batch.get("index") != 0:
                raise OneApiError("orphan live batch part")
            pending = (comparable, [], time.monotonic())
            self._pending[group] = pending
        if comparable != pending[0]:
            raise OneApiError("mixed live batch cut")
        parts = pending[1]
        if channel == "glydeBatchEnd":
            if batch.get("index", -1) is not None or len(parts) != len(declarations):
                raise OneApiError("incomplete live batch")
            if owner_group and {d["channel"] for d in declarations} == expected:
                self._commit_owner(parts, initial=self._account is None)
            elif not owner_group:
                data = parts[0]["data"]
                i = int(group.rsplit("-", 1)[1])
                if book_from_wire(data).symbol != self.config.symbols[i]:
                    raise OneApiError("book stream symbol mismatch")
                self._emit(parts[0])
            self._groups[group] = meta
            del self._pending[group]
            self._changed.set()
        else:
            if (
                type(batch["index"]) is not int
                or batch["index"] != len(parts)
                or channel != declarations[len(parts)]["channel"]
            ):
                raise OneApiError("duplicate or reordered live batch part")
            parts.append(frame)

    def _commit_owner(self, frames: list[JsonObject], *, initial: bool) -> None:
        roots = {frame["channel"]: frame["data"] for frame in frames}
        for name in ("clearinghouseState", "openOrders", "userFills"):
            if roots[name]["user"].lower() != self.config.owner.lower():
                raise OneApiError("private stream owner mismatch")
        account_data = roots["clearinghouseState"]["clearinghouseState"]
        stamp = account_data["time"]
        if type(stamp) is not int or not 0 <= stamp <= 2**63 - 1:
            raise OneApiError("invalid account source timestamp")
        account = account_from_wire(account_data)
        orders = orders_from_wire(roots["openOrders"]["orders"])
        for order in orders:
            if counter(order.order_id) == 0 or order.quantity <= 0 or order.price <= 0:
                raise OneApiError("invalid streamed open order")
        if len({o.order_id for o in orders}) != len(orders):
            raise OneApiError("duplicate streamed order")
        updates = roots["orderUpdates"]
        if not isinstance(updates, list) or len(updates) > 4096:
            raise OneApiError("invalid order updates")
        terminal = set(self._terminal)
        for row in updates:
            oid = str(row["order"]["oid"])
            counter(oid)
            if row["status"] in {"filled", "canceled", "rejected", "expired"}:
                terminal.add(oid)
            elif row["status"] != "open":
                raise OneApiError("unknown streamed order status")
        fill_rows = roots["userFills"]["fills"]
        if not isinstance(fill_rows, list) or len(fill_rows) > 4096:
            raise OneApiError("invalid streamed fills")
        fills = []
        seen = dict(self._seen_fills)
        for row in fill_rows:
            oid = str(row["oid"])
            tid = str(row["tid"])
            counter(oid)
            counter(tid)
            identity = oid, tid
            if identity in seen:
                if seen[identity] != row:
                    raise OneApiError("conflicting streamed fill identity")
                continue
            if row["side"] not in {"B", "A"}:
                raise OneApiError("invalid streamed fill side")
            quantity, price = decimal(row["sz"]), decimal(row["px"])
            if quantity <= 0 or price <= 0:
                raise OneApiError("invalid streamed fill economics")
            fill = Fill(
                row["coin"],
                "buy" if row["side"] == "B" else "sell",
                quantity,
                price,
                oid,
                decimal(row["fee"]),
                row.get("cloid"),
                tid,
            )
            seen[identity] = row
            if not initial and fill.symbol in self.config.symbols:
                fills.append(fill)
            if not any(order.order_id == oid for order in orders):
                terminal.add(oid)
        if len(seen) > 8192 or len(terminal) > 8192 or len(self._cancelled) > 8192:
            raise OneApiError("live private evidence budget exceeded")
        if any(order.order_id in terminal for order in orders):
            raise OneApiError("terminal order still present in live snapshot")
        self._account, self._account_time, self._orders = account, stamp, orders
        self._terminal, self._seen_fills = terminal, seen
        self.check()
        if self.metrics:
            self.metrics.account_cut(account.received_at, stamp / 1000)
        self._emit({"channel": "mmOwner", "fills": fills})
