"""One-shot public diagnostics: never sign, place orders or expose credential values."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

import httpx

from .client import OneApiClient, OneApiError
from .config import load_config
from .strategies.factory import validate_strategy_config


async def check(path: Path, *, report: Callable[[str], object] = print) -> int:
    try:
        selected = load_config(path)
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
        if type(market["szDecimals"]) is not int or not 0 <= market["szDecimals"] <= 6:
            raise ValueError("invalid market precision")
    except (
        OneApiError,
        httpx.InvalidURL,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        StopIteration,
    ):
        report("Public info: FAIL (unavailable or configured market metadata invalid)")
        return 1
    report("Public info: OK")
    report(
        "Public checks only; key validity, private permissions and live execution are not verified."
    )
    return 0 if healthy else 1
