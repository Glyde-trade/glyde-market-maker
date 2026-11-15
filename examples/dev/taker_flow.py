#!/usr/bin/env python3
"""Bounded synthetic taker flow for an owned LOCAL One API test stack only."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import random
from contextlib import asynccontextmanager
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from glyde_mm.client import OneApiClient, OneApiError, OutcomeUnknown
from glyde_mm.config import Config, decimal, load_config
from glyde_mm.models import OrderRequest
from glyde_mm.runner import _price_tick


def emit(message: str) -> None:
    print(message, flush=True)


def require_loopback(origin: str) -> None:
    parsed = urlsplit(origin)
    try:
        local = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        local = parsed.hostname == "localhost"
    if (
        not local
        or parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("taker flow refuses non-loopback origins")


@asynccontextmanager
async def local_client(config: Config):
    # Local development traffic must not inherit an outbound environment proxy.
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as http:
        async with OneApiClient(config, http_client=http) as client:
            yield client


async def run_flow(
    config: Config,
    *,
    count: int = 20,
    interval: float = 3,
    quantity: Decimal = Decimal("0.0001"),
    max_order_notional: Decimal = Decimal(25),
    max_total_notional: Decimal = Decimal(2500),
    seed: int = 7,
    client_factory=local_client,
    sleep=asyncio.sleep,
    report=emit,
) -> dict:
    require_loopback(config.base_url)
    if config.mode != "live" or len(config.symbols) != 1:
        raise ValueError("use an explicit single-symbol local live test config")
    if type(count) is not int or not 1 <= count <= 1000 or not 0.25 <= interval <= 60:
        raise ValueError("count must be 1..1000 and interval 0.25..60 seconds")
    for value, maximum in (
        (quantity, Decimal(1)),
        (max_order_notional, Decimal(25)),
        (max_total_notional, Decimal(5000)),
    ):
        if not value.is_finite() or not 0 < value <= maximum:
            raise ValueError("local taker budget is invalid or exceeds its hard cap")
    rng = random.Random(seed)
    result = {"attempts": 0, "filled": 0, "rejected": 0, "unknown": 0, "submitted_notional": "0"}
    spent = Decimal(0)
    sides = []
    async with client_factory(config) as client:
        meta = await client.meta()
        market = next((row for row in meta["universe"] if row["name"] == config.symbol), None)
        if (
            market is None
            or type(market["szDecimals"]) is not int
            or not 0 <= market["szDecimals"] <= 6
        ):
            raise ValueError("local market precision unavailable")
        precision = market["szDecimals"]
        step = Decimal(1).scaleb(-precision)
        if quantity % step:
            raise ValueError("quantity does not fit market precision")
        for index in range(count):
            if not sides:
                sides = ["buy", "sell"]
                rng.shuffle(sides)
            side = sides.pop()
            book = await client.book(config.symbol)
            if book.symbol != config.symbol or book.mid is None:
                raise ValueError("local book unavailable")
            tick = _price_tick(book, precision)
            price = book.asks[0].price if side == "buy" else book.bids[0].price
            price *= 1 + Decimal(rng.choice((1, 2, 3))) / 10000 * (1 if side == "buy" else -1)
            price = (price / tick).to_integral_value(
                rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR
            ) * tick
            notional = price * quantity
            if notional > max_order_notional or spent + notional > max_total_notional:
                raise ValueError("local taker notional budget exhausted before dispatch")
            original = "0x" + uuid4().hex
            order = OrderRequest(
                config.symbol, side, quantity, price, original, time_in_force="Ioc"
            )
            result["attempts"] += 1
            spent += notional
            result["submitted_notional"] = str(spent)
            report(f"SUBMIT original={original} side={side} quantity={quantity} price={price}")
            try:
                receipts = await client.place_orders([order])
            except OutcomeUnknown:
                report(f"UNKNOWN original={original}; STOP, do not replay or re-sign")
                raise
            if len(receipts) != 1:
                raise OutcomeUnknown("local taker receipt count unknown")
            receipt = receipts[0]
            if receipt.client_order_id not in (None, original):
                raise OutcomeUnknown("local taker receipt identity unknown")
            if receipt.status == "filled":
                result["filled"] += 1
            elif receipt.status == "rejected":
                result["rejected"] += 1
                report(f"REFUSED original={original}; STOP, no retry")
                raise OneApiError("local taker refused")
            else:
                # An IOC must never leave a quote. Reconcile its exact identity manually.
                raise OutcomeUnknown("local IOC was not definitively terminal")
            report(
                f"LOCAL TAKER {index + 1}/{count} {side} {quantity} {config.symbol}: "
                f"terminal fill receipt original={original}"
            )
            if index + 1 < count:
                await sleep(interval)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--confirm-local-test", action="store_true")
    parser.add_argument("--orders", type=int, default=20)
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument("--quantity", default="0.0001")
    parser.add_argument("--max-order-notional", default="25")
    parser.add_argument("--max-total-notional", default="2500")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        require_loopback(config.base_url)
        if not args.confirm_local_test:
            raise ValueError("explicit --confirm-local-test is required")
        result = asyncio.run(
            run_flow(
                config,
                count=args.orders,
                interval=args.interval,
                quantity=decimal(args.quantity),
                max_order_notional=decimal(args.max_order_notional),
                max_total_notional=decimal(args.max_total_notional),
                seed=args.seed,
            )
        )
        print(f"LOCAL TAKER SUMMARY {result}")
        return 0
    except (ValueError, OSError, OneApiError, TimeoutError, KeyboardInterrupt):
        print("Local taker stopped; inspect the original outcome. No automatic retry.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
