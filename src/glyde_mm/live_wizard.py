"""A bounded first live session, after diagnostics and the identical paper preset."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import yaml

from .client import OneApiClient
from .config import Config, load_config
from .doctor import check
from .preflight import summary
from .presets import strategy_preset
from .report import summarize
from .strategies.factory import validate_strategy_config
from .version import build_identity

PREVIEW_SECONDS = 120
LIVE_SECONDS = 1800


def _public_values(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _public_values(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_public_values(item) for item in value]
    return value


def _write_private(path: Path, text: str) -> None:
    with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


async def conservative_config(source: Config, *, client_factory=OneApiClient) -> Config:
    if source.mode != "live" or source.markets:
        raise ValueError("live-wizard requires an explicit single-symbol live config")
    policy = validate_strategy_config(source.strategy, source.strategy_config)
    async with client_factory(source) as client:
        metadata = await client.meta()
        book = await client.book(source.symbol)
    precision = next(
        row["szDecimals"] for row in metadata["universe"] if row["name"] == source.symbol
    )
    if (
        type(precision) is not int
        or not 0 <= precision <= 6
        or book.symbol != source.symbol
        or not book.bids
        or not book.asks
        or book.mid is None
        or not book.mid.is_finite()
        or book.mid <= 0
        or not -1
        <= time.time() - book.timestamp_ms / 1000
        <= min(5, float(source.risk.stale_book_seconds))
    ):
        raise ValueError("a fresh market and valid size precision are required")
    risk = replace(
        source.risk,
        max_position=min(source.risk.max_position, Decimal(25) / book.mid),
        max_order_notional=min(source.risk.max_order_notional, Decimal(25)),
        max_total_notional=min(source.risk.max_total_notional or Decimal(25), Decimal(25)),
        max_daily_loss=min(source.risk.max_daily_loss, Decimal(5)),
        max_orders_per_minute=min(source.risk.max_orders_per_minute, 60),
        stale_book_seconds=min(source.risk.stale_book_seconds, Decimal(5)),
        account_age_seconds=min(source.risk.account_age_seconds, Decimal(10)),
    )
    values = strategy_preset(
        source.strategy,
        "conservative",
        book.mid,
        risk.max_order_notional,
        risk.max_position,
        reference_source=getattr(policy, "reference_source", "glyde"),
    )
    if (
        Decimal(values["order_amount"]).quantize(
            Decimal(1).scaleb(-precision), rounding="ROUND_DOWN"
        )
        <= 0
    ):
        raise ValueError("caps are too small for market quantity precision")
    return replace(source, risk=risk, strategy_config=values)


async def first_live_session(
    path: Path,
    *,
    run_session,
    input_fn=None,
    report=print,
    client_factory=OneApiClient,
    doctor_fn=check,
) -> int:
    state = path.parent / ".glyde-mm"
    directory = state / "live-wizard" / f"session-{uuid4().hex}"
    directory.mkdir(mode=0o700, parents=True)
    result = {
        **build_identity(),
        "time": datetime.now(UTC).isoformat(),
        "outcome": "failed",
        "phase": "setup",
        "preview_seconds": PREVIEW_SECONDS,
        "live_limit_seconds": LIVE_SECONDS,
        "live_requested": False,
        "runner_exit_without_cleanup_error": False,
        "positions_closed": False,
    }
    logs = directory / "logs"
    try:
        selected = await conservative_config(load_config(path), client_factory=client_factory)
        effective_path = directory / "config.yaml"
        public = _public_values(asdict(selected))
        public.pop("markets")
        _write_private(effective_path, yaml.safe_dump(public))
        report("FIRST LIVE SESSION: Conservative preset; custom strategy settings are replaced.")
        report(summary(selected))
        report(f"Exact proposed config: {effective_path}")
        report("Venue minimum size/notional is unavailable; no risk cap is raised to meet it.")
        result["phase"] = "doctor"
        if await doctor_fn(effective_path, private=True, report=report) != 0:
            return 1
        result["phase"] = "preview"
        report("PAPER PREVIEW: 120 seconds, the same strategy/settings and no real orders.")
        complete = await run_session(
            replace(selected, mode="paper"),
            state,
            duration_seconds=PREVIEW_SECONDS,
            log_directory=logs,
        )
        if not complete:
            result["outcome"] = "cancelled"
            return 1
        result["phase"] = "confirmation"
        report(summary(selected))
        report("LIVE: up to 30 minutes, then stop quoting and attempt cancellation.")
        report(
            "Positions are not flattened. Loss caps are not guarantees; use a dedicated account."
        )
        try:
            confirmed = (input_fn or input)(f"Type {selected.symbol} to start LIVE: ")
        except EOFError:
            result["outcome"] = "cancelled"
            return 1
        if confirmed != selected.symbol:
            result["outcome"] = "cancelled"
            return 1
        result["phase"] = "live"
        result["live_requested"] = True
        automatic = await run_session(
            selected,
            state,
            duration_seconds=LIVE_SECONDS,
            log_directory=logs,
        )
        result["runner_exit_without_cleanup_error"] = True
        result["outcome"] = "auto_stopped" if automatic else "stopped"
        return 0
    except Exception:
        report("First session refused or failed; no automatic retry. Check the report and venue.")
        return 1
    finally:
        if logs.exists():
            try:
                result["diagnostics"] = summarize(logs)
            except (ValueError, OSError):
                result["diagnostics"] = "unavailable"
        result["ended"] = datetime.now(UTC).isoformat()
        final_report = directory / "report.json"
        _write_private(final_report, json.dumps(result, indent=2) + "\n")
        report(f"FIRST SESSION {result['outcome']} | report={final_report}")
