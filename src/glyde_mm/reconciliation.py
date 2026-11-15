"""Offline quote-lifetime counterfactual against captured public/private WS facts."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from .client import trades_from_wire
from .config import decimal
from .models import CancelRequest, OrderRequest
from .paper import PaperExchange
from .public_tape import decode_event

ZERO = Decimal(0)


def _id(value):
    if type(value) not in (int, str) or not re.fullmatch(r"[0-9]{1,64}", str(value)):
        raise ValueError("invalid recorded identity")
    return str(value)


def _number(value):
    if len(str(value)) > 128:
        raise ValueError("execution number exceeds capture bounds")
    number = decimal(value)
    if abs(number.adjusted()) > 64:
        raise ValueError("execution number exceeds capture bounds")
    return number


def _positive(value):
    number = _number(value)
    if number <= 0:
        raise ValueError("invalid execution economics")
    return number


def read_session(path: Path):
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("session exceeds 16 MiB")
    with path.open() as source:
        content = source.read(16 * 1024 * 1024 + 1)
    if len(content) > 16 * 1024 * 1024:
        raise ValueError("session exceeds 16 MiB")
    rows = [json.loads(line, parse_float=Decimal) for line in content.splitlines()]
    if not 3 <= len(rows) <= 100000:
        raise ValueError("session event count unavailable")
    header, events = rows[0], rows[1:]
    if (
        header.get("format") != "glyde.execution-session"
        or type(header.get("version")) is not int
        or header["version"] != 1
        or not isinstance(header.get("symbol"), str)
        or not 1 <= len(header["symbol"]) <= 64
        or type(header.get("size_decimals")) is not int
        or not 0 <= header["size_decimals"] <= 6
        or not re.fullmatch(r"0x[0-9a-fA-F]{40}", header.get("user", ""))
    ):
        raise ValueError("invalid execution session header")
    previous = -1
    for event in events:
        stamp = event["received_at_ms"]
        if type(stamp) is not int or not 0 <= stamp <= 2**63 - 1 or stamp < previous:
            raise ValueError("session receipt time regressed")
        previous = stamp
    end = events[-1]
    if (
        end.get("type") != "end"
        or end.get("public_complete") is not True
        or end.get("private_complete") is not True
        or previous <= events[0]["received_at_ms"]
        or previous - events[0]["received_at_ms"] > 3600000
        or any(event.get("type") == "end" for event in events[:-1])
    ):
        raise ValueError("complete public/private capture coverage required")
    return header, events


def _actual_fills(header, events, orders):
    actual = defaultdict(list)
    seen = {}
    for event in events:
        if event["type"] != "private":
            continue
        frame = event["frame"]
        data = frame["data"]
        if frame.get("channel") != "userFills" or data["user"].lower() != header["user"].lower():
            raise ValueError("private fill owner/channel mismatch")
        if ("isSnapshot" in data and type(data["isSnapshot"]) is not bool) or not isinstance(
            data.get("fills"), list
        ):
            raise ValueError("invalid private fill frame")
        for fill in data["fills"]:
            oid = _id(fill["oid"])
            if oid not in orders:
                continue  # Other actors' orders are never attributed to this quote schedule.
            request = orders[oid]
            if (
                fill["coin"] != header["symbol"]
                or fill["side"] not in {"B", "A"}
                or ("buy" if fill["side"] == "B" else "sell") != request["side"]
                or fill.get("cloid", request["client_order_id"]) != request["client_order_id"]
                or type(fill["time"]) is not int
                or fill["time"] < events[0]["received_at_ms"]
                or fill["time"] > event["received_at_ms"] + 1000
                or fill["time"] > events[-1]["received_at_ms"]
            ):
                raise ValueError("private fill binding/time mismatch")
            quantity, price = _positive(fill["sz"]), _positive(fill["px"])
            limit = _positive(request["price"])
            if (request["side"] == "buy" and price > limit) or (
                request["side"] == "sell" and price < limit
            ):
                raise ValueError("private fill violates original limit price")
            fee = _number(fill["fee"]) if fill.get("feeToken") == "USDC" and "fee" in fill else None
            normalized = (quantity, price, fee, fill["time"], fill.get("feeToken"), fill.get("fee"))
            identity = oid, _id(fill["tid"])
            if identity in seen:
                if seen[identity] != normalized:
                    raise ValueError("conflicting duplicate private fill")
                continue
            seen[identity] = normalized
            actual[oid].append((quantity, price, fee))
    return actual


async def reconcile_session(path: Path) -> dict:
    try:
        header, events = read_session(path)
        orders = {}
        for event in events:
            if event["type"] == "order":
                oid, request = _id(event["receipt"]["order_id"]), event["order"]
                if (
                    oid in orders
                    or event["receipt"]["status"] != "resting"
                    or not re.fullmatch(r"0x[0-9a-fA-F]{32}", request["client_order_id"])
                    or event["receipt"].get("client_order_id", request["client_order_id"])
                    != request["client_order_id"]
                    or request["symbol"] != header["symbol"]
                    or request["side"] not in {"buy", "sell"}
                    or request.get("time_in_force", "Alo") != "Alo"
                    or request.get("reduce_only", False) is not False
                ):
                    raise ValueError("definite original maker quote receipts required")
                orders[oid] = request
        if not orders:
            raise ValueError("quote lifetimes unavailable; do not infer them from fills")
        actual = _actual_fills(header, events, orders)
        now = events[0]["received_at_ms"]
        paper = PaperExchange(clock_ms=lambda: now)
        paper.use_trade_tape()
        mapped = {}
        private_seen = False
        public_trades_seen = False
        try:
            for event in events:
                now = event["received_at_ms"]
                kind = event["type"]
                if kind == "public":
                    frame = event["frame"]
                    decoded = decode_event(
                        {
                            "received_at_ms": now,
                            "source": "glyde",
                            "channel": frame["channel"],
                            "data": frame["data"],
                        },
                        header["symbol"],
                    )
                    if decoded.book:
                        for level in (*decoded.book.bids, *decoded.book.asks):
                            _positive(level.price)
                            _positive(level.quantity)
                        await paper.on_book(decoded.book)
                    else:
                        public_trades_seen = True
                        for trade in trades_from_wire(frame["data"]):
                            _positive(trade.price)
                            _positive(trade.quantity)
                            await paper.on_trade(trade)
                elif kind == "order":
                    request = event["order"]
                    order = OrderRequest(
                        header["symbol"],
                        request["side"],
                        _positive(request["quantity"]),
                        _positive(request["price"]),
                        request["client_order_id"],
                    )
                    step = Decimal(1).scaleb(-header["size_decimals"])
                    if order.quantity % step:
                        raise ValueError("recorded quote quantity precision mismatch")
                    result = (await paper.place_orders([order]))[0]
                    if result.status != "resting":
                        raise ValueError("recorded quote not replayable against captured book")
                    mapped[_id(event["receipt"]["order_id"])] = result.order_id
                elif kind == "cancel":
                    oid = _id(event["order_id"])
                    if (
                        oid not in mapped
                        or _id(event["receipt"]["order_id"]) != oid
                        or event["receipt"]["status"] not in {"cancelled", "already_closed"}
                    ):
                        raise ValueError("definite original cancellation receipt required")
                    await paper.cancel_orders([CancelRequest(header["symbol"], mapped[oid])])
                elif kind == "private":
                    private_seen = True
                elif kind != "end":
                    raise ValueError("unsupported session event")
        finally:
            await paper.cancel_all()
        if not private_seen or not public_trades_seen:
            raise ValueError("public trade/private WS coverage unavailable")
        rows = []
        for oid, request in orders.items():
            simulated = [
                (fill.quantity, fill.price, ZERO)
                for fill in paper.fills
                if fill.order_id == mapped[oid]
            ]
            observed = actual[oid]
            paper_qty = sum((row[0] for row in simulated), ZERO)
            live_qty = sum((row[0] for row in observed), ZERO)
            if live_qty > _positive(request["quantity"]):
                raise ValueError("private fills exceed the original quote")
            matched = min(live_qty, paper_qty)
            paper_vwap = (
                sum((q * p for q, p, _ in simulated), ZERO) / paper_qty if paper_qty else None
            )
            live_vwap = sum((q * p for q, p, _ in observed), ZERO) / live_qty if live_qty else None
            slippage = (
                (live_vwap - paper_vwap) * (1 if request["side"] == "buy" else -1)
                if matched
                else None
            )
            fee = (
                sum((row[2] for row in observed), ZERO)
                if all(row[2] is not None for row in observed)
                else None
            )
            rows.append(
                {
                    "order_id": oid,
                    "side": request["side"],
                    "live_quantity": str(live_qty),
                    "paper_quantity": str(paper_qty),
                    "matched_quantity": str(matched),
                    "missed_live_quantity": str(max(ZERO, paper_qty - live_qty)),
                    "live_only_quantity": str(max(ZERO, live_qty - paper_qty)),
                    "live_vwap": str(live_vwap) if live_vwap is not None else None,
                    "paper_vwap": str(paper_vwap) if paper_vwap is not None else None,
                    "adverse_slippage_per_base": str(slippage) if slippage is not None else None,
                    "matched_slippage_usdc": str(slippage * matched)
                    if slippage is not None
                    else None,
                    "live_fee_usdc": str(fee) if fee is not None else None,
                }
            )
        total_fees = (
            sum((decimal(row["live_fee_usdc"]) for row in rows), ZERO)
            if all(row["live_fee_usdc"] is not None for row in rows)
            else None
        )
        return {
            "schema": 1,
            "symbol": header["symbol"],
            "basis": "post-ack quote lifetimes; approximate public-tape queue; "
            "capture-declared coverage",
            "paper_fee_basis": "zero; no fee schedule inferred",
            "orders": rows,
            "matched_quantity": str(sum((decimal(row["matched_quantity"]) for row in rows), ZERO)),
            "matched_slippage_usdc": str(
                sum(
                    (
                        decimal(row["matched_slippage_usdc"])
                        for row in rows
                        if row["matched_slippage_usdc"] is not None
                    ),
                    ZERO,
                )
            )
            if any(decimal(row["matched_quantity"]) > ZERO for row in rows)
            else None,
            "fee_impact_usdc": str(total_fees) if total_fees is not None else None,
            "missed_live_quantity": str(
                sum((decimal(row["missed_live_quantity"]) for row in rows), ZERO)
            ),
            "live_only_quantity": str(
                sum((decimal(row["live_only_quantity"]) for row in rows), ZERO)
            ),
        }
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError("invalid reconciliation capture") from error
