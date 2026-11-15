"""One-shot public diagnostics: never sign, place orders or expose credential values."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import httpx

from .client import OneApiClient, OneApiError, OneApiHTTPError
from .config import load_config
from .runner import _price_tick
from .strategies.factory import validate_strategy_config


async def check(
    path: Path, *, private: bool = False, report: Callable[[str], object] = print
) -> int:
    try:
        selected = load_config(path)
        if private and selected.mode != "live":
            raise ValueError("private checks require explicit live configuration")
        validate_strategy_config(selected.strategy, selected.strategy_config)
        for name in (selected.private_key_env, selected.read_api_key_env):
            if not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z_0-9]{0,127}", name):
                raise ValueError("invalid environment variable name")
        if selected.mode == "live" and not all(
            re.fullmatch(r"0x[0-9a-fA-F]{40}", value or "")
            for value in (selected.owner, selected.vault_address)
        ):
            raise ValueError("live mode requires registered account and vault addresses")
    except (OSError, ValueError, TypeError, AttributeError):
        report("Configuration: FAIL (check YAML, strategy settings and live account addresses)")
        return 1
    report(f"Configuration: OK ({selected.mode})")
    healthy = True
    for label, name in (
        ("Trading key", selected.private_key_env),
        ("Read key", selected.read_api_key_env),
    ):
        present = bool(os.environ.get(name))
        requirement = (
            "required in live mode" if selected.mode == "live" else "optional in paper mode"
        )
        report(f"{label}: {'present' if present else 'absent'} ({requirement})")
        if selected.mode == "live" and not present:
            healthy = False
    try:
        async with OneApiClient(selected) as client:
            metadata = await client.meta()
            market = next(row for row in metadata["universe"] if row["name"] == selected.symbol)
            size = market["szDecimals"]
            if type(size) is not int or not 0 <= size <= 6:
                raise ValueError("invalid market precision")
            book = await client.book(selected.symbol)
            if (
                book.symbol != selected.symbol
                or not book.bids
                or not book.asks
                or not -1
                <= time.time() - book.timestamp_ms / 1000
                <= float(selected.risk.stale_book_seconds)
            ):
                raise ValueError("configured market has no fresh book")
            tick = _price_tick(book, size)
            if abs(tick.adjusted()) > 30:
                raise ValueError("market price outside supported diagnostic range")
            report("Public info: OK")
            report(f"Quantity step: {Decimal(1).scaleb(-size):f}")
            report(f"Runner price tick at current midpoint: {tick:f}")
            report("Venue minimum order size/notional: unavailable in public metadata")
            if private:
                if not os.environ.get(selected.read_api_key_env):
                    report("Private reads: FAIL (read key absent)")
                    healthy = False
                else:
                    try:
                        await client.account()
                        await client.open_orders()
                    except (OneApiError, ValueError, TypeError, KeyError, AttributeError):
                        report(
                            "Private reads: FAIL (account or open-orders permission unavailable)"
                        )
                        healthy = False
                    else:
                        report("Private reads: OK (account and open orders)")
    except (
        OneApiError,
        httpx.InvalidURL,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        StopIteration,
    ) as error:
        if isinstance(error, OneApiHTTPError) and error.status_code in {401, 404}:
            report(
                f"Public info: FAIL (requested One API read unavailable at this endpoint: "
                f"HTTP {error.status_code}; One API may not be deployed yet)"
            )
        else:
            report(
                "Public info: FAIL (unavailable, stale book or configured market metadata invalid)"
            )
        return 1
    if not private:
        report(
            "Public checks only; private read permissions are not verified (live: use --private)."
        )
    report("Signing/trading permissions and live execution are not verified; no order was sent.")
    return 0 if healthy else 1
