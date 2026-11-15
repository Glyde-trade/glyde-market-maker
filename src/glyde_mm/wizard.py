"""A paper-first configuration wizard that never asks for a signing secret."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml


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
    amount = ask("Order amount in base units (check the current market price)", "0.0001", _decimal)
    spread = ask(
        "Bid/ask spread fraction (0.002 = 0.2%)",
        "0.002",
        lambda value: _decimal(value, maximum=Decimal("1")),
    )
    maximum_order = ask("Maximum notional per order in USDC", "25", _decimal)
    maximum_position = ask("Maximum absolute position in base units", "0.001", _decimal)
    maximum_loss = ask("Stop after daily account equity loss in USDC", "5", _decimal)
    private_key_env = ask(
        "Signing key environment variable NAME (used only in live mode)",
        "GLYDE_API_PRIVATE_KEY",
        environment,
    )
    values = {
        "mode": "paper",
        "symbol": market,
        "base_url": "https://api.glyde.trade/v1",
        "strategy": "pure_market_making",
        "paper_balance": "1000",
        "private_key_env": private_key_env,
        "read_api_key_env": "GLYDE_READ_API_KEY",
        "strategy_config": {
            "order_amount": amount,
            "bid_spread": spread,
            "ask_spread": spread,
            "order_refresh_time": 30,
            "order_levels": 1,
            "hanging_orders_enabled": False,
        },
        "risk": {
            "max_position": maximum_position,
            "max_order_notional": maximum_order,
            "max_daily_loss": maximum_loss,
            "stale_book_seconds": "5",
            "account_age_seconds": "10",
        },
    }
    if Decimal(amount) > Decimal(maximum_position):
        raise ValueError("Order amount exceeds maximum position; no configuration was written.")
    # Exclusive open also closes the check/create race without following a new symlink.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            yaml.safe_dump(values, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    print_fn(
        "Paper configuration saved. Keep keys in your environment; never paste them into YAML."
    )
    print_fn(
        "Size limits can refuse quotes. Check exchange minimum size and current notional first."
    )
    return path
