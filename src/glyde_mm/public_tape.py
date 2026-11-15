"""Bounded public book/trade recordings; no account data or trading authority."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from pathlib import Path

from .book_history import check_minutes
from .client import OneApiClient, book_from_wire, trades_from_wire
from .config import Config
from .models import Book, Trade
from .reference import HYPERLIQUID_PUBLIC_URL


@dataclass(frozen=True)
class TapeEvent:
    received_at_ms: int
    source: str
    book: Book | None = None
    trades: tuple[Trade, ...] = ()


def decode_event(row: dict, symbol: str) -> TapeEvent:
    if set(row) != {"received_at_ms", "source", "channel", "data"}:
        raise ValueError("invalid tape event")
    receipt, source, channel, data = (
        row[key] for key in ("received_at_ms", "source", "channel", "data")
    )
    if type(receipt) is not int or receipt < 0 or source not in {"glyde", "hyperliquid"}:
        raise ValueError("invalid tape source/time")
    if channel == "l2Book":
        if type(data["time"]) is not int or not 0 <= data["time"] <= receipt + 1000:
            raise ValueError("invalid source time")
        book = book_from_wire(data)
        if book.symbol != symbol or not book.bids or not book.asks:
            raise ValueError("invalid recorded market")
        return TapeEvent(receipt, source, replace(book, received_at=receipt / 1000))
    if source != "glyde" or channel != "trades":
        raise ValueError("unsupported tape channel")
    trades = trades_from_wire(data)
    if any(trade.symbol != symbol or trade.timestamp_ms > receipt + 1000 for trade in trades):
        raise ValueError("invalid trade market/time")
    return TapeEvent(receipt, source, trades=trades)


def read_tape(path: Path, symbol: str) -> tuple[int, list[TapeEvent]]:
    if path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("tape exceeds 256 MiB")
    try:
        with path.open() as handle:
            header = json.loads(handle.readline(1024))
            if (
                not isinstance(header, dict)
                or header.get("format") != "glyde.public-tape"
                or type(header.get("version")) is not int
                or header["version"] != 1
                or header.get("symbol") != symbol
                or type(header.get("size_decimals")) is not int
                or not 0 <= header["size_decimals"] <= 6
            ):
                raise ValueError("incompatible public tape")
            events = []
            cuts: dict[str, int] = {}
            for line in handle:
                if len(line) > 1024 * 1024 or len(events) >= 100000:
                    raise ValueError("tape exceeds event limits")
                event = decode_event(json.loads(line), symbol)
                if events and event.received_at_ms < events[-1].received_at_ms:
                    raise ValueError("tape receipt time regressed")
                if event.book:
                    if event.book.timestamp_ms < cuts.get(event.source, -1):
                        raise ValueError("tape book source time regressed")
                    cuts[event.source] = event.book.timestamp_ms
                events.append(event)
        if (
            not events
            or "glyde" not in cuts
            or not any(event.book is None for event in events)
            or events[-1].received_at_ms <= events[0].received_at_ms
            or events[-1].received_at_ms - events[0].received_at_ms > 3610000
        ):
            raise ValueError(
                "tape needs books, a trade stream and positive coverage"
                " of at most 60 minutes plus 10 seconds"
            )
        return header["size_decimals"], events
    except (KeyError, TypeError, AttributeError, OverflowError):
        raise ValueError("malformed public tape") from None


def _wire(event: TapeEvent) -> dict:
    book = event.book
    return {
        "received_at_ms": event.received_at_ms,
        "source": event.source,
        "channel": "l2Book" if book else "trades",
        "data": {
            "coin": book.symbol,
            "time": book.timestamp_ms,
            "levels": [
                [{"px": str(p.price), "sz": str(p.quantity)} for p in side]
                for side in (book.bids, book.asks)
            ],
        }
        if book
        else [
            {
                "coin": t.symbol,
                "side": "B" if t.side == "buy" else "A",
                "px": str(t.price),
                "sz": str(t.quantity),
                "time": t.timestamp_ms,
                "tid": t.trade_id,
            }
            for t in event.trades
        ],
    }


async def record_tape(
    config: Config,
    output: Path,
    minutes: int,
    *,
    include_hl: bool = False,
    client_factory=OneApiClient,
) -> int:
    check_minutes(minutes)
    if config.markets:
        raise ValueError("tape recording requires a single-symbol config")
    queue: asyncio.Queue = asyncio.Queue(maxsize=128)
    tasks = []
    streams = []

    async def receive(source, stream):
        try:
            async for frame in stream:
                if frame.get("channel") in {"l2Book", "trades"}:
                    await queue.put((source, time.time_ns() // 1_000_000, frame))
        except Exception:
            await queue.put((source, 0, None))
        else:
            await queue.put((source, 0, None))

    async with AsyncExitStack() as resources:
        public = replace(config, mode="paper", owner="", vault_address=None)
        glyde = await resources.enter_async_context(client_factory(public))
        meta = await glyde.meta()
        market = next((m for m in meta["universe"] if m["name"] == config.symbol), None)
        if (
            market is None
            or type(market["szDecimals"]) is not int
            or not 0 <= market["szDecimals"] <= 6
        ):
            raise ValueError("market precision unavailable")
        sources = [
            ("glyde", glyde, [{"type": c, "coin": config.symbol} for c in ("l2Book", "trades")])
        ]
        if include_hl:
            hl = await resources.enter_async_context(
                client_factory(replace(public, base_url=HYPERLIQUID_PUBLIC_URL))
            )
            sources.append(("hyperliquid", hl, [{"type": "l2Book", "coin": config.symbol}]))
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        count, first, complete = 0, None, False
        with os.fdopen(descriptor, "w") as handle:
            handle.write(
                json.dumps(
                    {
                        "format": "glyde.public-tape",
                        "version": 1,
                        "symbol": config.symbol,
                        "size_decimals": market["szDecimals"],
                    }
                )
                + "\n"
            )
            try:
                for name, client, subscriptions in sources:
                    stream = client.stream(subscriptions)
                    streams.append(stream)
                    tasks.append(asyncio.create_task(receive(name, stream)))
                async with asyncio.timeout(minutes * 60 + 10):
                    while True:
                        source, receipt, frame = await queue.get()
                        if frame is None:
                            raise ValueError("public tape stream ended before coverage")
                        event = decode_event(
                            {
                                "source": source,
                                "received_at_ms": receipt,
                                "channel": frame["channel"],
                                "data": frame["data"],
                            },
                            config.symbol,
                        )
                        encoded = json.dumps(_wire(event)) + "\n"
                        if (
                            count >= 100000
                            or len(encoded) > 1024 * 1024
                            or handle.tell() + len(encoded) > 256 * 1024 * 1024
                        ):
                            raise ValueError("public tape exceeds recording limits")
                        handle.write(encoded)
                        count += 1
                        first = receipt if first is None else first
                        if receipt - first >= minutes * 60000:
                            complete = True
                            break
            except TimeoutError:
                pass
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for stream in streams:
                    await stream.aclose()
                handle.flush()
                os.fsync(handle.fileno())
        if not complete:
            raise ValueError("public tape coverage incomplete; partial recording retained")
        read_tape(output, config.symbol)
        return count
