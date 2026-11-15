"""Private bounded JSONL diagnostics. Never capture messages, requests or credentials."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .version import build_identity

_FIELDS = {
    "strategy_started": set(),
    "progress": {"book_messages", "pending_orders", "paper_fills"},
    "run_ended": {"outcome"},
}
_OUTCOMES = {"completed", "stopped", "cancelled", "risk_stop", "transport_error", "error"}
_STRATEGIES = {
    "pure_market_making",
    "avellaneda_market_making",
    "grid",
    "inventory_skewed",
    "plugin",
}


class RunLog:
    def __init__(
        self,
        directory: Path,
        *,
        mode: str,
        strategy: str,
        max_bytes: int = 1024 * 1024,
        backups: int = 3,
    ) -> None:
        if max_bytes < 1024 or not 1 <= backups <= 16:
            raise ValueError("invalid run log limits")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("run log directory must be an owned directory")
        directory.chmod(0o700)
        self.run_id = uuid4().hex
        self.directory = directory / f"run-{self.run_id}"
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "events.jsonl"
        self.max_bytes, self.backups = max_bytes, backups
        self.sequence = 0
        self.identity = build_identity()
        if strategy.startswith("plugin:"):
            strategy = "plugin"
        self.header = {
            "mode": mode if mode in {"paper", "live"} else "unknown",
            "strategy": strategy if strategy in _STRATEGIES else "unknown",
            **self.identity,
        }
        self._file = self._open()
        self._write("run_header", self.header)

    def _open(self):
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        return os.fdopen(descriptor, "w", encoding="utf-8")

    def _write(self, event: str, fields: dict) -> None:
        self.sequence += 1
        row = {
            "schema": 1,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            **fields,
        }
        self._file.write(json.dumps(row, separators=(",", ":")) + "\n")
        self._file.flush()

    def emit(self, event: str, **fields: object) -> None:
        if event not in _FIELDS or set(fields) != _FIELDS[event]:
            raise ValueError("unsupported diagnostic fields")
        if event == "progress" and any(
            type(value) is not int or not 0 <= value <= 2**63 - 1 for value in fields.values()
        ):
            raise ValueError("invalid diagnostic counts")
        if event == "run_ended" and fields["outcome"] not in _OUTCOMES:
            raise ValueError("invalid diagnostic outcome")
        if self._file.tell() + 512 > self.max_bytes:
            self._file.close()
            for index in range(self.backups, 0, -1):
                source = self.path if index == 1 else self.directory / f"events.{index - 1}.jsonl"
                target = self.directory / f"events.{index}.jsonl"
                if source.exists():
                    source.replace(target)
            self._file = self._open()
            self._write("run_header", self.header)
        self._write(event, fields)

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> RunLog:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
