"""A paper-first configuration wizard that never asks for a signing secret."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from .presets import STRATEGIES, TRADEOFFS, strategy_preset


def _decimal(value: str, *, maximum: Decimal | None = None) -> str:
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("Enter a positive decimal number.") from None
    if not number.is_finite() or number <= 0 or (maximum is not None and number >= maximum):
        raise ValueError("Enter a positive decimal within the allowed range.")
    return str(number)


def run_wizard(
    output: str | Path = "glyde-mm.yaml",
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], object] = print,
    interactive: bool = True,
) -> Path:
    """Write one exclusive 0600 YAML file; noninteractive callers get paper defaults."""
    path = Path(output)
    if path.exists():
        raise FileExistsError("Configuration exists; choose another output path.")

    def ask(prompt: str, default: str, validate: Callable[[str], str]) -> str:
        if not interactive:
            return validate(default)
        while True:
            value = input_fn(f"{prompt} [{default}]: ").strip() or default
            try:
                return validate(value)
            except ValueError as error:
                print_fn(str(error))

    def symbol(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,63}", value):
            raise ValueError("Use a market symbol such as BTC.")
        return value

    def environment(value: str) -> str:
        if not re.fullmatch(r"[A-Z_][A-Z_0-9]{0,127}", value):
            raise ValueError("Enter an environment variable NAME, never a private key.")
        return value

    print_fn("Start in PAPER mode: orders and balances are simulated; no trading key is needed.")
    market = ask("Market", "BTC", symbol)

    def choice(value: str, options: tuple[str, ...]) -> str:
        selected = value.lower()
        if selected.isdigit() and 1 <= int(selected) <= len(options):
            selected = options[int(selected) - 1]
        if selected not in options:
            raise ValueError("Choose a listed name or number.")
        return selected

    print_fn("Strategies: " + ", ".join(f"{i} {name}" for i, name in enumerate(STRATEGIES, 1)))
    strategy = ask("Strategy", STRATEGIES[0], lambda value: choice(value, STRATEGIES))
    for index, (name, explanation) in enumerate(TRADEOFFS.items(), 1):
        print_fn(f"{index} {name.title()}: {explanation}")
    preset = ask("Preset", "conservative", lambda value: choice(value, tuple(TRADEOFFS)))
    reference = ask(
        "Reference market price in USDC (update this to today's price)", "100000", _decimal
    )
    maximum_order = ask("Maximum notional per order in USDC", "25", _decimal)
    maximum_position = ask("Maximum absolute position in base units", "0.001", _decimal)
    maximum_loss = ask("Stop after daily account equity loss in USDC", "5", _decimal)
    private_key_env = ask(
        "Signing key environment variable NAME (used only in live mode)",
        "GLYDE_API_PRIVATE_KEY",
        environment,
    )
    reference_source = "glyde"
    if strategy == "pure_market_making":
        reference_source = ask(
            "Quote center: glyde or hyperliquid public mid",
            "glyde",
            lambda value: choice(value, ("glyde", "hyperliquid")),
        )
    values = {
        "mode": "paper",
        "symbol": market,
        "base_url": "https://api.glyde.trade/v1",
        "strategy": strategy,
        "paper_balance": "1000",
        "private_key_env": private_key_env,
        "read_api_key_env": "GLYDE_READ_API_KEY",
        "strategy_config": strategy_preset(
            strategy,
            preset,
            Decimal(reference),
            Decimal(maximum_order),
            Decimal(maximum_position),
            reference_source=reference_source,
        ),
        "risk": {
            "max_position": maximum_position,
            "max_order_notional": maximum_order,
            "max_daily_loss": maximum_loss,
            "max_orders_per_minute": 60,
            "stale_book_seconds": "5",
            "account_age_seconds": "10",
        },
    }
    # Exclusive open also closes the check/create race without following a new symlink.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(
                f"# {preset.title()} paper preset; sizing reference price {reference} USDC.\n"
            )
            handle.write(
                "# Prices move: runtime risk caps still apply. Edit this YAML to customize.\n"
            )
            yaml.safe_dump(values, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    print_fn(
        "Paper configuration saved. Keep keys in your environment; never paste them into YAML."
    )
    print_fn("Amounts share the position cap across levels; PMM sizing allows for twofold skew.")
    print_fn("Prices and venue minimums can refuse quotes; presets never raise your risk caps.")
    if strategy in {"avellaneda_market_making", "inventory_skewed"}:
        print_fn("Avellaneda waits for 31 consecutive one-second samples with price movement.")
    return path
