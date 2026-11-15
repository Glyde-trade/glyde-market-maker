"""Opt-in loopback-only Prometheus observations; never reads an exchange."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

from .config import Config

if TYPE_CHECKING:
    from .models import Account, Book
    from .paper import PaperExchange

_REASONS = frozenset(
    {
        "venue_rejected",
        "pretrade_refused",
        "outcome_unknown",
        "stale_book",
        "stale_book_source",
        "stale_account",
        "invalid_book",
        "invalid_account",
        "max_position",
        "max_order_notional",
        "max_total_notional",
        "daily_loss",
        "order_rate_limit",
        "client_closed",
        "other",
    }
)


class Metrics:
    def __init__(
        self,
        config: Config,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        collect: bool = False,
    ) -> None:
        self.enabled = config.metrics_enabled or collect
        self._listen = config.metrics_enabled
        self.port = config.metrics_port
        self._symbols = {symbol: str(i) for i, symbol in enumerate(config.symbols)}
        self._clock, self._wall = clock, wall_clock
        self._lock = threading.Lock()
        self._counts = {
            key: 0 for key in ("quotes_placed", "quotes_cancelled", "gaps", "reconnects")
        }
        self._refused = dict.fromkeys(_REASONS, 0)
        self._fills = dict.fromkeys(self._symbols.values(), 0)
        self._inventory = dict.fromkeys(self._symbols.values(), math.nan)
        self._books: dict[str, tuple[float, float]] = {}
        self._account_cut: tuple[float, float] | None = None
        self._realized = self._unrealized = self._headroom = math.nan
        self._connections = 0
        self._paper_fill_index = 0
        self.server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def count(self, event: str, amount: int = 1) -> None:
        if self.enabled and event in self._counts:
            with self._lock:
                self._counts[event] += amount

    def refused(self, reason: str, amount: int) -> None:
        if self.enabled:
            with self._lock:
                self._refused[reason if reason in _REASONS else "other"] += amount

    def fill(self, symbol: str) -> None:
        if self.enabled and symbol in self._symbols:
            with self._lock:
                self._fills[self._symbols[symbol]] += 1

    def paper_fills(self, paper: PaperExchange) -> None:
        if not self.enabled:
            return
        for fill in paper.fills[self._paper_fill_index :]:
            self.fill(fill.symbol)
        self._paper_fill_index = len(paper.fills)

    def connected(self) -> None:
        if self.enabled:
            with self._lock:
                if self._connections:
                    self._counts["reconnects"] += 1
                self._connections += 1

    def book(self, book: Book) -> None:
        if self.enabled and book.symbol in self._symbols:
            with self._lock:
                self._books[self._symbols[book.symbol]] = book.received_at, book.timestamp_ms / 1000

    def account_cut(self, received_at: float, source_seconds: float) -> None:
        if self.enabled:
            with self._lock:
                self._account_cut = received_at, source_seconds

    def snapshot(
        self,
        account: Account | None,
        baseline: Decimal | None,
        cap: Decimal,
        *,
        paper: PaperExchange | None = None,
        initial_balance: Decimal = Decimal(0),
    ) -> None:
        if not self.enabled or account is None:
            return
        with self._lock:
            self._inventory = {
                index: float(account.positions.get(symbol, Decimal(0)))
                for symbol, index in self._symbols.items()
            }
            self._headroom = (
                float(cap - (baseline - account.equity)) if baseline is not None else math.nan
            )
            self._realized = float(paper._cash - initial_balance) if paper is not None else math.nan
            self._unrealized = (
                float(account.equity - paper._cash)
                if paper is not None
                else (
                    float(account.unrealized_pnl)
                    if account.unrealized_pnl is not None
                    else math.nan
                )
            )

    def render(self) -> bytes:
        with self._lock:
            rows: list[str] = []

            def emit(
                name: str, kind: str, description: str, values: list[tuple[str, float | int]]
            ) -> None:
                full = "glyde_mm_" + name
                rows.extend((f"# HELP {full} {description}", f"# TYPE {full} {kind}"))
                for labels, value in values:
                    text = (
                        "NaN"
                        if isinstance(value, float) and not math.isfinite(value)
                        else str(value)
                    )
                    rows.append(f"{full}{labels} {text}")

            for name, count in self._counts.items():
                emit(
                    name + "_total",
                    "counter",
                    "Session observations; reset on a new runner session.",
                    [("", count)],
                )
            emit(
                "quotes_refused_total",
                "counter",
                "Definite refusals by fixed category.",
                [
                    (f'{{reason="{reason}"}}', count)
                    for reason, count in sorted(self._refused.items())
                ],
            )
            emit(
                "fills_total",
                "counter",
                "Identified fill events, including partial fills.",
                [(f'{{market="{index}"}}', count) for index, count in self._fills.items()],
            )
            emit(
                "inventory",
                "gauge",
                "Signed base quantity by configured market index.",
                [(f'{{market="{index}"}}', value) for index, value in self._inventory.items()],
            )
            for name, value in (
                ("realized_pnl", self._realized),
                ("unrealized_pnl", self._unrealized),
                ("daily_loss_headroom", self._headroom),
            ):
                emit(name, "gauge", "USDC; NaN means unavailable, not zero.", [("", value)])
            for domain in ("arrival", "source"):
                offset = 0 if domain == "arrival" else 1
                now = self._clock() if domain == "arrival" else self._wall()
                samples = [
                    (
                        f'{{market="{index}"}}',
                        max(0.0, now - self._books[index][offset])
                        if index in self._books
                        else math.nan,
                    )
                    for index in self._symbols.values()
                ]
                emit(
                    f"book_{domain}_age_seconds",
                    "gauge",
                    "Age of last committed book; NaN before baseline.",
                    samples,
                )
                emit(
                    f"account_{domain}_age_seconds",
                    "gauge",
                    "Age of last committed owner cut; NaN before baseline.",
                    [
                        (
                            "",
                            max(0.0, now - self._account_cut[offset])
                            if self._account_cut
                            else math.nan,
                        )
                    ],
                )
            return ("\n".join(rows) + "\n").encode()

    def result_counts(self) -> dict[str, object]:
        with self._lock:
            return {
                "placed": self._counts["quotes_placed"],
                "cancelled": self._counts["quotes_cancelled"],
                "refused_by_reason": {
                    key: value for key, value in sorted(self._refused.items()) if value
                },
                "fills": sum(self._fills.values()),
            }

    def __enter__(self) -> Metrics:
        if not self._listen:
            return self
        metrics = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                port = metrics.server.server_port if metrics.server else 0
                host = self.headers.get("Host", "")
                origin = self.headers.get("Origin")
                if host not in {f"127.0.0.1:{port}", f"localhost:{port}"} or (
                    origin and origin != "http://" + host
                ):
                    self.send_error(403)
                    return
                if self.path != "/metrics":
                    self.send_error(404)
                    return
                data = metrics.render()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        server = self.server

        def serve() -> None:
            server.serve_forever(poll_interval=0.05)

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self._thread:
            self._thread.join(timeout=1)
