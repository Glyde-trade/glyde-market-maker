"""Public market checks and a short-lived receipt bound to the effective configuration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, DecimalException
from pathlib import Path

from .client import OneApiClient, OneApiError
from .config import Config
from .models import Book
from .order_rules import order_rules
from .risk import RiskLimits
from .strategies.factory import validate_strategy_config
from .strategies.grid import GridConfig
from .strategies.plugin import PluginDefinition
from .strategies.pure_market_making import PureMarketMakingConfig
from .transport import ClientFactory, JsonObject

RECEIPT_SECONDS = 300
CHECK_VERSION = 2


class MarketPreflightRequired(ValueError):
    """No recent successful public check exists for this exact effective configuration."""


def config_hash(config: Config) -> str:
    def encode(value: object) -> str:
        if isinstance(value, Decimal):
            return str(value)
        raise TypeError("configuration is not serializable")

    data = json.dumps(
        {"checks": CHECK_VERSION, "config": asdict(config)},
        default=encode,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(data.encode()).hexdigest()


def _receipt_path(config: Config, directory: Path) -> Path:
    return directory / f"preflight-{config_hash(config)}.json"


def require_preflight(config: Config, directory: Path) -> None:
    try:
        path = _receipt_path(config, directory)
        if path.stat().st_size > 4096:
            raise ValueError
        record = json.loads(path.read_text())
        checked = record["checked_at"]
        if (
            record["config_hash"] != config_hash(config)
            or record["passed"] is not True
            or type(checked) not in (float, int)
            or not 0 <= time.time() - checked <= RECEIPT_SECONDS
        ):
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError):
        raise MarketPreflightRequired(
            "Run glyde-mm preflight with this exact config; PASS expires after 5 minutes."
        ) from None


def _write_receipt(config: Config, directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as target:
            temporary = Path(target.name)
            json.dump(
                {"config_hash": config_hash(config), "passed": True, "checked_at": time.time()},
                target,
            )
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, _receipt_path(config, directory))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _market_fixes(config: Config, market: JsonObject, book: Book) -> tuple[list[str], str, Decimal]:
    from .runner import _price_tick

    fixes = []
    symbol = config.symbol
    size = market.get("szDecimals")
    if type(size) is not int or not 0 <= size <= 6 or market.get("isDelisted", False):
        return (
            [f"{symbol}: public meta needs an active market and szDecimals in 0..6."],
            "",
            Decimal(0),
        )
    if (
        book.symbol != symbol
        or not book.bids
        or not book.asks
        or book.mid is None
        or not -1 <= time.time() - book.timestamp_ms / 1000 <= float(config.risk.stale_book_seconds)
    ):
        return [f"{symbol}: obtain a fresh two-sided public l2Book."], "", Decimal(0)
    try:
        rules = order_rules(market)
    except (ValueError, DecimalException) as error:
        return [f"{symbol}: {error}"], "", Decimal(0)
    step, tick = rules.quantity_step, _price_tick(book, size)
    policy = validate_strategy_config(config.strategy, config.strategy_config)
    if isinstance(policy, PluginDefinition):
        raise ValueError("built-in strategy required")
    if isinstance(policy, GridConfig):
        quantities = [policy.order_amount] * policy.levels
        spacing = (policy.upper_price - policy.lower_price) / (policy.levels - 1)
        bids = [policy.lower_price + spacing * i for i in range(policy.levels)]
        asks = list(bids)
        prices = [policy.upper_price] * policy.levels
    else:
        quantities = [
            policy.order_amount + policy.order_level_amount * i for i in range(policy.order_levels)
        ]
        # Public preflight checks nominal unskewed prices, not a future private/model state.
        bids = [
            book.mid
            * (
                1
                - max(
                    policy.minimum_spread,
                    getattr(policy, "bid_spread", policy.minimum_spread)
                    + policy.order_level_spread * i,
                )
            )
            for i in range(len(quantities))
        ]
        asks = [
            book.mid
            * (
                1
                + max(
                    policy.minimum_spread,
                    getattr(policy, "ask_spread", policy.minimum_spread)
                    + policy.order_level_spread * i,
                )
            )
            for i in range(len(quantities))
        ]
        prices = [max(book.asks[0].price, price) for price in asks]
        if isinstance(policy, PureMarketMakingConfig) and policy.inventory_skew_enabled:
            quantities = [q * 2 for q in quantities]
    nominal_quantities = [
        policy.order_amount
        + (0 if isinstance(policy, GridConfig) else policy.order_level_amount) * i
        for i in range(len(quantities))
    ]
    for i, (quantity, bid, ask) in enumerate(zip(nominal_quantities, bids, asks, strict=True), 1):
        for side, price, rounding in (("bid", bid, ROUND_FLOOR), ("ask", ask, ROUND_CEILING)):
            rounded = (price / tick).to_integral_value(rounding=rounding) * tick
            if not rules.valid_price(rounded):
                fixes.append(
                    f"{symbol}: level {i} {side} price violates native priceTick/decimal/"
                    "significant-figure rules; adjust quote settings."
                )
            if quantity * rounded < rules.minimum_notional:
                fixes.append(
                    f"{symbol}: level {i} {side} notional {quantity * rounded:f} is below "
                    f"minimumNotionalUsd {rules.minimum_notional:f}; increase order_amount "
                    "within your risk caps."
                )
    base = policy.order_amount
    if base % step != 0:
        fixes.append(f"{symbol}: order_amount must be a multiple of {step:f} base.")
    total = Decimal(0)
    notional = Decimal(0)
    for i, (quantity, price) in enumerate(zip(quantities, prices, strict=True), 1):
        if quantity % step != 0:
            fixes.append(f"{symbol}: level {i} quantity must be a multiple of {step:f} base.")
        reference = max(
            book.asks[0].price, (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        )
        cost = quantity * reference
        if cost > config.risk.max_order_notional:
            fixes.append(
                f"{symbol}: reduce level {i} quantity; {cost:f} exceeds max_order_notional "
                f"{config.risk.max_order_notional:f} at the current book."
            )
        total += quantity
        notional += cost
    if total > config.risk.max_position:
        fixes.append(
            f"{symbol}: reduce order_amount/levels; one-sided quantity {total:f} exceeds "
            f"max_position {config.risk.max_position:f} (including possible size skew)."
        )
    details = (
        f"{symbol}: quantity_step={step:f}, minimum_notional_usd={rules.minimum_notional:f}, "
        f"tick={rules.price_tick:f}, one_side={total:f} (nominal current-book check)"
    )
    return fixes, details, notional


async def check(
    config: Config,
    directory: Path,
    *,
    report: Callable[[str], object] = print,
    client_factory: ClientFactory | None = None,
) -> int:
    fixes = []
    details = []
    path = _receipt_path(config, directory)
    path.unlink(missing_ok=True)
    try:
        RiskLimits.from_config(config.risk)
    except ValueError:
        report("FIX: use positive risk caps and freshness limits within supported ranges.")
        return 1
    # Iteration reads names only, unlike getenv or mapping membership on os.environ.
    present = set(iter(os.environ))
    for name in (config.private_key_env, config.read_api_key_env):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z_0-9]{0,127}", name):
            fixes.append("Use uppercase credential environment-variable names in the config.")
            continue
        details.append(f"{name}={'present' if name in present else 'absent'}")
        if config.mode == "live" and name not in present:
            fixes.append(
                f"Set environment variable {name} before a live run (value is never inspected)."
            )
    if config.strategy.startswith("plugin:"):
        fixes.append(
            "Select a built-in strategy; arbitrary plugin sizing cannot be preflighted publicly."
        )
    else:
        try:
            total = Decimal(0)
            factory = client_factory or OneApiClient
            async with factory(config) as client:
                meta = await client.meta()
                for selected in config.market_configs():
                    matches = [row for row in meta["universe"] if row["name"] == selected.symbol]
                    if len(matches) != 1:
                        fixes.append(
                            f"{selected.symbol}: select one active market advertised in meta."
                        )
                        continue
                    book = await client.book(selected.symbol)
                    errors, summary, notional = _market_fixes(selected, matches[0], book)
                    fixes.extend(errors)
                    if summary:
                        details.append(summary)
                    total += notional
            if (
                config.risk.max_total_notional is not None
                and total > config.risk.max_total_notional
            ):
                fixes.append(
                    f"Reduce levels across markets; one-sided total {total:f} exceeds "
                    f"max_total_notional {config.risk.max_total_notional:f} at the current book."
                )
        except (OneApiError, ValueError, TypeError, KeyError, AttributeError, DecimalException):
            fixes.append(
                "Fix invalid strategy/market metadata or restore public meta "
                "and fresh l2Book access."
            )
    if fixes:
        for line in details:
            report(line)
        for fix in dict.fromkeys(fixes):
            report(f"FIX: {fix}")
        return 1
    _write_receipt(config, directory)
    report(f"PASS {config_hash(config)} | " + " | ".join(details))
    return 0
