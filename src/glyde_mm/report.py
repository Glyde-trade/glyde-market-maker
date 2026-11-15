"""Offline, bounded support summaries of allowlisted run diagnostics."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime
from itertools import pairwise
from pathlib import Path

from .runlog import _FIELDS, _OUTCOMES, _STRATEGIES

_RUN = re.compile(r"run-([0-9a-f]{32})")
_SEGMENT = re.compile(r"events(?:\.([1-9][0-9]?))?\.jsonl")
_COMMON = {"schema", "run_id", "sequence", "time", "event"}
_HEADER = {"mode", "strategy", "version", "commit"}
_MAX_FILE = 1024 * 1024
_MAX_TOTAL = 64 * _MAX_FILE


def _directory(path: Path) -> None:
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError("Report input must be a real log directory.")


def _validate(row: object, run_id: str) -> dict:
    if not isinstance(row, dict):
        raise ValueError("Invalid diagnostic record.")
    event = row.get("event")
    if not isinstance(event, str) or event not in {*_FIELDS, "run_header"}:
        raise ValueError("Invalid diagnostic event.")
    fields = _HEADER if event == "run_header" else _FIELDS[event]
    if set(row) != _COMMON | fields:
        raise ValueError("Unexpected diagnostic fields.")
    if (
        type(row["schema"]) is not int
        or row["schema"] != 1
        or row["run_id"] != run_id
        or type(row["sequence"]) is not int
        or not 1 <= row["sequence"] <= 2**63 - 1
        or not isinstance(row["time"], str)
        or len(row["time"]) > 40
    ):
        raise ValueError("Invalid diagnostic identity.")
    stamp = datetime.fromisoformat(row["time"])
    if stamp.tzinfo is None:
        raise ValueError("Diagnostic time requires a timezone.")
    # Re-render the timestamp; never copy unvalidated free text into a support ticket.
    row["time"] = stamp.isoformat()
    if event == "run_header":
        if (
            row["mode"] not in ("paper", "live", "unknown")
            or row["strategy"] not in (*_STRATEGIES, "unknown")
            or not isinstance(row["version"], str)
            or not re.fullmatch(r"[A-Za-z0-9.+_-]{1,64}", row["version"])
            or not isinstance(row["commit"], str)
            or not re.fullmatch(r"[0-9a-f]{40}|unknown", row["commit"])
        ):
            raise ValueError("Invalid diagnostic build header.")
    elif event == "progress":
        if any(type(row[key]) is not int or not 0 <= row[key] <= 2**63 - 1 for key in fields):
            raise ValueError("Invalid diagnostic counters.")
    elif event == "run_ended" and row["outcome"] not in tuple(_OUTCOMES):
        raise ValueError("Invalid diagnostic outcome.")
    return row


def _read_run(path: Path, budget: list[int]) -> dict:
    _directory(path)
    match = _RUN.fullmatch(path.name)
    if match is None:
        raise ValueError("Choose a run directory or its parent logs directory.")
    run_id = match[1]
    segments = []
    for item in path.iterdir():
        match = _SEGMENT.fullmatch(item.name)
        if match:
            if match[1] is not None and int(match[1]) > 16:
                raise ValueError("Too many diagnostic segments.")
            segments.append(item)
            if len(segments) > 17:
                raise ValueError("Too many diagnostic segments.")
    if not segments:
        raise ValueError("No diagnostic segments found.")
    rows = []
    identity = None
    for segment in segments:
        descriptor = os.open(segment, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE:
                raise ValueError("Invalid or oversized diagnostic segment.")
            budget[0] += info.st_size
            if budget[0] > _MAX_TOTAL:
                raise ValueError("Report input exceeds the size limit.")
            first = True
            consumed = 0
            while line := handle.readline(4097):
                consumed += len(line)
                if len(line) > 4096 or consumed > _MAX_FILE:
                    raise ValueError("Diagnostic segment exceeds the size limit.")
                row = _validate(json.loads(line), run_id)
                if first:
                    if row["event"] != "run_header":
                        raise ValueError("Diagnostic segment has no build header.")
                    header = {key: row[key] for key in _HEADER}
                    if identity is not None and header != identity:
                        raise ValueError("Conflicting diagnostic build headers.")
                    identity = header
                    first = False
                elif row["event"] == "run_header":
                    raise ValueError("Unexpected diagnostic build header.")
                rows.append(row)
            if first:
                raise ValueError("Empty diagnostic segment.")
    rows.sort(key=lambda row: row["sequence"])
    if any(b["sequence"] != a["sequence"] + 1 for a, b in pairwise(rows)):
        raise ValueError("Diagnostic sequence has a gap or duplicate.")
    if any(row["event"] == "run_ended" for row in rows[:-1]):
        raise ValueError("Diagnostic records follow an exit event.")
    progress = next((row for row in reversed(rows) if row["event"] == "progress"), None)
    return {
        "run_id": run_id,
        **identity,
        "first_observed_at": rows[0]["time"],
        "last_observed_at": rows[-1]["time"],
        "outcome": rows[-1].get("outcome", "incomplete"),
        "history_truncated": rows[0]["sequence"] != 1,
        "segments": len(segments),
        "last_progress": {key: progress[key] for key in _FIELDS["progress"]} if progress else None,
    }


def summarize(directory: Path) -> dict:
    """Return safe metadata only; never access the network, config or credential store."""
    _directory(directory)
    if _RUN.fullmatch(directory.name):
        paths = [directory]
    else:
        paths = []
        for item in directory.iterdir():
            if _RUN.fullmatch(item.name):
                paths.append(item)
                if len(paths) > 50:
                    raise ValueError("Choose one run directory when there are more than 50 runs.")
    if not paths:
        raise ValueError("No diagnostic runs found.")
    budget = [0]
    try:
        return {"schema": 1, "runs": [_read_run(path, budget) for path in sorted(paths)]}
    except (TypeError, UnicodeError, RecursionError) as error:
        raise ValueError("Malformed diagnostic input.") from error
