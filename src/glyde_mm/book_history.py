"""Local public-book capture. No historical depth is synthesized from a current snapshot."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .client import OneApiClient, book_from_wire
from .config import Config
from .models import Book
from .transport import ClientFactory


@dataclass(frozen=True)
class Observation:
    received_at_ms: int
    book: Book


def check_minutes(minutes: int) -> None:
    if type(minutes) is not int or not 1 <= minutes <= 60:
        raise ValueError("minutes must be an integer from 1 to 60")


async def record_books(
    config: Config, output: Path, minutes: int, *, client_factory: ClientFactory = OneApiClient
) -> int:
    if config.markets:
        raise ValueError("record/replay requires a single-symbol configuration")
    check_minutes(minutes)
    config = replace(config, mode="paper")
    count = 0
    first_receipt = None
    complete = False
    async with client_factory(config) as source:
        meta = await source.meta()
        market = next((row for row in meta["universe"] if row["name"] == config.symbol), None)
        if (
            market is None
            or type(market["szDecimals"]) is not int
            or not 0 <= market["szDecimals"] <= 6
        ):
            raise ValueError("market size precision unavailable")
        header = {
            "format": "glyde.public-book",
            "version": 1,
            "symbol": config.symbol,
            "size_decimals": market["szDecimals"],
        }
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        stream = source.stream([{"type": "l2Book", "coin": config.symbol}])
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(header) + "\n")
            try:
                async with asyncio.timeout(minutes * 60 + 10):
                    async for frame in stream:
                        if frame.get("channel") != "l2Book":
                            continue
                        if count >= 100000:
                            raise ValueError("capture exceeds 100000 books; use a shorter window")
                        data = frame["data"]
                        book = book_from_wire(data)
                        if book.symbol != config.symbol:
                            raise ValueError("book market mismatch")
                        # Only public depth fields are retained, never arbitrary envelope metadata.
                        row: dict[str, Any] = {
                            "received_at_ms": time.time_ns() // 1_000_000,
                            "book": {
                                "coin": book.symbol,
                                "time": book.timestamp_ms,
                                "levels": [
                                    [{"px": str(p.price), "sz": str(p.quantity)} for p in side]
                                    for side in (book.bids, book.asks)
                                ],
                            },
                        }
                        handle.write(json.dumps(row) + "\n")
                        handle.flush()
                        count += 1
                        receipt = int(row["received_at_ms"])
                        if first_receipt is None:
                            first_receipt = receipt
                        if receipt - first_receipt >= minutes * 60000:
                            complete = True
                            break
            except TimeoutError:
                pass
            finally:
                await stream.aclose()
                handle.flush()
                os.fsync(handle.fileno())
    if not complete:
        raise ValueError("capture ended before requested book coverage; recording retained")
    return count


def _read_window(path: Path, symbol: str, minutes: int) -> tuple[int, int, list[Observation]]:
    check_minutes(minutes)
    if path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("book recording exceeds 256 MiB")
    observations: list[Observation] = []
    with path.open() as handle:
        header = json.loads(handle.readline(1024))
        if not isinstance(header, dict):
            raise ValueError("incompatible book recording")
        if type(header.get("version")) is not int:
            raise ValueError("incompatible book recording")
        if (header.get("format"), header.get("version"), header.get("symbol")) != (
            "glyde.public-book",
            1,
            symbol,
        ):
            raise ValueError("incompatible book recording")
        size = header.get("size_decimals")
        if type(size) is not int or not 0 <= size <= 6:
            raise ValueError("invalid recorded size precision")
        previous_source = -1
        for line in handle:
            if len(line) > 1024 * 1024 or len(observations) >= 100000:
                raise ValueError("recording exceeds replay limits")
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get("book"), dict):
                raise ValueError("malformed book recording")
            receipt = int(row["received_at_ms"])
            data = row["book"]
            if type(receipt) is not int or receipt < 0 or type(data["time"]) is not int:
                raise ValueError("invalid recorded timestamps")
            book = book_from_wire(data)
            if (
                book.symbol != symbol
                or book.timestamp_ms < previous_source
                or (observations and receipt < observations[-1].received_at_ms)
            ):
                raise ValueError("recorded market or time regressed")
            if not book.bids or not book.asks:
                raise ValueError("recorded book is empty")
            previous_source = book.timestamp_ms
            observations.append(Observation(receipt, replace(book, received_at=receipt / 1000)))
    if not observations:
        raise ValueError("recording is empty")
    start = observations[-1].received_at_ms - minutes * 60000
    if observations[0].received_at_ms > start:
        raise ValueError("recording does not cover the requested minutes")
    baseline = max(i for i, row in enumerate(observations) if row.received_at_ms <= start)
    return size, start, observations[baseline:]


def read_window(path: Path, symbol: str, minutes: int) -> tuple[int, int, list[Observation]]:
    try:
        return _read_window(path, symbol, minutes)
    except (KeyError, TypeError, OverflowError):
        raise ValueError("malformed book recording") from None
