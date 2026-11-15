"""Local display-only snapshots. This module never creates a trading client."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from .config import Config
from .models import OpenOrder, OrderRequest
from .paper import PaperExchange
from .risk import RiskGuard

_STATUSES = {
    "starting",
    "running",
    "completed",
    "stopped",
    "cancelled",
    "risk_stop",
    "transport_error",
    "error",
}
_FIELDS = {
    "schema",
    "run_id",
    "mode",
    "strategy",
    "symbols",
    "updated_at_ms",
    "status",
    "risk_stop",
    "equity",
    "pnl",
    "pnl_basis",
    "inventory",
    "quotes",
    "quotes_basis",
    "fills",
    "fill_count",
}


class SnapshotPublisher:
    def __init__(self, path: Path, config: Config, run_id: str) -> None:
        self.path = path
        self.balance = config.paper_balance
        self.state: dict[str, Any] = {
            "schema": 1,
            "run_id": run_id,
            "mode": config.mode,
            "strategy": config.strategy,
            "symbols": list(config.symbols),
            "updated_at_ms": 0,
            "status": "starting",
            "risk_stop": None,
            "equity": None,
            "pnl": None,
            "pnl_basis": "paper_initial_balance"
            if config.mode == "paper"
            else "daily_equity_change",
            "inventory": None,
            "quotes": [],
            "quotes_basis": "tracked_orders",
            "fills": None,
            "fill_count": None,
        }
        self._write()

    def _write(self) -> None:
        self.state["updated_at_ms"] = time.time_ns() // 1_000_000
        descriptor, temporary = tempfile.mkstemp(prefix=".dashboard-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(self.state, output, separators=(",", ":"))
                output.write("\n")
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    async def publish(self, guard: RiskGuard, *, paper: PaperExchange | None = None) -> None:
        account = guard.account_state
        orders: Sequence[OrderRequest | OpenOrder] = list(guard.pending.values())
        baseline = guard._equity_start
        if paper is not None:
            account = await paper.account()
            orders = await paper.open_orders()
            baseline = self.balance
            self.state["fills"] = [
                {
                    "symbol": row.symbol,
                    "side": row.side,
                    "quantity": str(row.quantity),
                    "price": str(row.price),
                }
                for row in paper.fills[-100:]
            ]
            self.state["fill_count"] = len(paper.fills)
            self.state["quotes_basis"] = "simulator_open_orders"
        self.state.update(
            status="running",
            risk_stop=None if guard.halted == "closed" else guard.halted,
            equity=str(account.equity) if account else None,
            pnl=str(account.equity - baseline) if account and baseline is not None else None,
            inventory={key: str(value) for key, value in account.positions.items()}
            if account
            else None,
            quotes=[
                {
                    "symbol": row.symbol,
                    "side": row.side,
                    "quantity": str(row.quantity),
                    "price": str(row.price),
                }
                for row in orders[:200]
            ],
        )
        self._write()

    def finish(self, outcome: str) -> None:
        self.state["status"] = outcome
        self._write()


def _text(value: object, *, limit: int = 128) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise ValueError("invalid dashboard state")
    return value


def _number(value: object) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", _text(value)):
        raise ValueError("invalid dashboard number")
    return _text(value)


def _rows(value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError("invalid dashboard rows")
    result = []
    for row in value:
        if row["side"] not in {"buy", "sell"}:
            raise ValueError("invalid dashboard side")
        result.append(
            {
                "symbol": _text(row["symbol"], limit=64),
                "side": row["side"],
                "quantity": _number(row["quantity"]),
                "price": _number(row["price"]),
            }
        )
    return result


def read_snapshot(path: Path, *, now_ms: int | None = None) -> dict[str, Any]:
    """Read only an owned private snapshot, never expose arbitrary JSON fields."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor) as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > 262144
            ):
                raise ValueError("invalid dashboard file")
            content = source.read(262145)
            if len(content) > 262144:
                raise ValueError("dashboard file too large")
            row = json.loads(content)
        if (
            type(row["schema"]) is not int
            or row["schema"] != 1
            or row["mode"] not in {"paper", "live"}
            or row["status"] not in _STATUSES
        ):
            raise ValueError("invalid dashboard header")
        selected = {key: row[key] for key in _FIELDS}
        for field in ("run_id", "strategy", "pnl_basis", "quotes_basis"):
            selected[field] = _text(row[field])
        if not isinstance(row["symbols"], list) or not 1 <= len(row["symbols"]) <= 50:
            raise ValueError("invalid dashboard symbols")
        selected["symbols"] = [_text(symbol, limit=64) for symbol in row["symbols"]]
        if row["risk_stop"] is not None:
            selected["risk_stop"] = _text(row["risk_stop"])
        for field in ("equity", "pnl"):
            selected[field] = _number(row[field])
        inventory = row["inventory"]
        if inventory is not None:
            if not isinstance(inventory, dict) or len(inventory) > 50:
                raise ValueError("invalid dashboard inventory")
            selected["inventory"] = {
                _text(key, limit=64): _number(value) for key, value in inventory.items()
            }
        for field in ("quotes", "fills"):
            selected[field] = _rows(row[field])
        for field in ("updated_at_ms", "fill_count"):
            if row[field] is None and field == "fill_count":
                continue
            if type(row[field]) is not int or not 0 <= row[field] <= 2**63 - 1:
                raise ValueError("invalid dashboard count")
        now_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        age = now_ms - row["updated_at_ms"]
        return {
            "available": True,
            "stale": row["status"] != "running" or not 0 <= age <= 5000,
            "age_ms": max(0, age),
            "snapshot": selected,
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"available": False, "stale": True, "snapshot": None}


_PAGE = Path(__file__).with_name("dashboard.html").read_bytes()


def make_server(path: Path, port: int = 8080) -> ThreadingHTTPServer:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid dashboard port")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            expected = {
                f"127.0.0.1:{cast(ThreadingHTTPServer, self.server).server_port}",
                f"localhost:{cast(ThreadingHTTPServer, self.server).server_port}",
            }
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if host not in expected or (origin and origin != "http://" + host):
                self.send_error(403)
                return
            route = urlsplit(self.path).path
            if route == "/":
                body, content_type = _PAGE, "text/html; charset=utf-8"
            elif route == "/state":
                body, content_type = json.dumps(read_snapshot(path)).encode(), "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(path: Path, port: int = 8080) -> None:
    with make_server(path, port) as server:
        print(f"Read-only dashboard: http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
