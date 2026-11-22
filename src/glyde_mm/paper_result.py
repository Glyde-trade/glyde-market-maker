"""A private, bounded paper-session result; no client or credential access."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .runlog import _STRATEGIES
from .version import build_identity

if TYPE_CHECKING:
    from .config import Config
    from .metrics import Metrics
    from .paper import PaperExchange


class PaperResult:
    def __init__(
        self, path: Path, config: Config, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if config.mode != "paper":
            raise ValueError("paper result requires paper mode")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self._file = os.fdopen(descriptor, "w", encoding="utf-8")
        self.path, self.clock = path, clock
        self.start = clock()
        self.initial = config.paper_balance
        self.peak = config.paper_balance
        self.drawdown = Decimal(0)
        self.realized = self.unrealized = Decimal(0)
        self.inventory: dict[str, str] = {}
        self.path_points: list[dict[str, Any]] = []
        self.path_truncated = False
        self.outcome = "setup_error"
        self.cleanup_confirmed = False
        self.pause_counts = {"stale": 0, "gap": 0, "disconnect": 0}
        self._pauses: set[str] = set()
        self.counts: dict[str, Any] = {}
        self.observed = False
        self.open_orders: int | None = None
        self.strategy = config.strategy if config.strategy in _STRATEGIES else "plugin"
        self.symbols = config.symbols

    async def observe(self, paper: PaperExchange, metrics: Metrics) -> None:
        account = await paper.account()
        self.observed = True
        self.realized = paper._cash - self.initial
        self.unrealized = account.equity - paper._cash
        self.peak = max(self.peak, account.equity)
        self.drawdown = max(self.drawdown, self.peak - account.equity)
        inventory = {
            symbol: str(account.positions.get(symbol, Decimal(0))) for symbol in self.symbols
        }
        self.inventory = inventory
        if not self.path_points or inventory != self.path_points[-1]["inventory"]:
            point = {"elapsed_seconds": round(self.clock() - self.start, 6), "inventory": inventory}
            if len(self.path_points) < 10000:
                self.path_points.append(point)
            else:
                self.path_points[-1] = point
                self.path_truncated = True
        self.open_orders = len(await paper.open_orders())
        metrics.paper_fills(paper)
        self.counts = metrics.result_counts()

    def pause(self, reason: str) -> None:
        if reason in self.pause_counts and reason not in self._pauses:
            self.pause_counts[reason] += 1
            self._pauses.add(reason)

    def resume(self, reason: str) -> None:
        self._pauses.discard(reason)

    def write(self, termination: str) -> dict[str, Any]:
        result = {
            "schema": 1,
            "mode": "paper",
            "strategy": self.strategy,
            **build_identity(),
            "outcome": self.outcome,
            "termination": termination,
            "uptime_seconds": round(self.clock() - self.start, 6),
            "quotes": {key: value for key, value in self.counts.items() if key != "fills"},
            "fills": self.counts.get("fills", 0),
            "open_quotes_at_end": self.open_orders,
            "inventory": self.inventory if self.observed else None,
            "inventory_path": self.path_points,
            "inventory_path_truncated": self.path_truncated,
            "realized_pnl_usdc": str(self.realized) if self.observed else None,
            "unrealized_pnl_usdc": str(self.unrealized) if self.observed else None,
            "max_drawdown_usdc": str(self.drawdown) if self.observed else None,
            "pause_counts": self.pause_counts,
            "pause_behavior": "disconnect_reconnects_after_resync; other pauses stop the session",
            "cleanup_confirmed": self.cleanup_confirmed,
            "pnl_model": "simulated public-tape execution; no fees, funding or live settlement",
        }
        try:
            json.dump(result, self._file, indent=2, allow_nan=False)
            self._file.write("\n")
            self._file.flush()
            os.fsync(self._file.fileno())
        finally:
            self._file.close()
        return result
