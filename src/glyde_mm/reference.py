"""Public Hyperliquid data is a quote reference, never an account or order authority."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

from .models import Book
from .risk import RiskViolation

HYPERLIQUID_PUBLIC_URL = "https://api.hyperliquid.xyz"


class ExternalReference:
    def __init__(
        self,
        symbol: str,
        *,
        max_deviation_bps: Decimal,
        skew_bps: Decimal,
        stale_seconds: Decimal,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.symbol = symbol
        self.max_deviation_bps, self.skew_bps = max_deviation_bps, skew_bps
        self.stale_seconds = float(stale_seconds)
        self.clock, self.wall_clock = clock, wall_clock
        self.book: Book | None = None

    def update(self, book: Book) -> None:
        if (
            book.symbol != self.symbol
            or type(book.timestamp_ms) is not int
            or book.timestamp_ms < 0
            or not math.isfinite(book.received_at)
        ):
            raise RiskViolation("reference_invalid")
        if self.book is not None:
            if (
                book.timestamp_ms < self.book.timestamp_ms
                or book.received_at < self.book.received_at
            ):
                raise RiskViolation("reference_regressed")
            if book.timestamp_ms == self.book.timestamp_ms:
                book = replace(book, received_at=min(book.received_at, self.book.received_at))
        self.book = book

    def center(self, glyde_book: Book) -> Decimal:
        book = self.book
        if book is None:
            raise RiskViolation("reference_unavailable")
        if (
            not 0 <= self.clock() - book.received_at <= self.stale_seconds
            or not -1 <= self.wall_clock() - book.timestamp_ms / 1000 <= self.stale_seconds
        ):
            raise RiskViolation("reference_stale")
        if (
            glyde_book.symbol != self.symbol
            or not book.bids
            or not book.asks
            or book.bids[0].price >= book.asks[0].price
        ):
            raise RiskViolation("reference_invalid")
        mid, glyde_mid = book.mid, glyde_book.mid
        if (
            mid is None
            or glyde_mid is None
            or not mid.is_finite()
            or not glyde_mid.is_finite()
            or mid <= 0
            or glyde_mid <= 0
        ):
            raise RiskViolation("reference_invalid")
        deviation = abs(glyde_mid - mid) / mid * 10000
        if deviation > self.max_deviation_bps:
            raise RiskViolation("reference_deviation")
        return mid * (1 + self.skew_bps / 10000)
