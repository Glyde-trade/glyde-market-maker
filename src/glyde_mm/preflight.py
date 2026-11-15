"""Retail startup summary and a read-only existing-exposure refusal."""

from __future__ import annotations

from decimal import Decimal

from .config import Config
from .models import TradingClient
from .risk import RiskViolation


class LivePreflightRefused(RiskViolation):
    """Existing symbol exposure needs explicit consent before managed cleanup exists."""


def summary(config: Config) -> str:
    caps = (
        str(config.risk.max_position)
        if not config.markets
        else ",".join(f"{m.symbol}:{m.risk.max_position}" for m in config.market_configs())
    )
    return (
        f"PRE-FLIGHT | mode={config.mode.upper()} | symbol={','.join(config.symbols)}"
        f" | max_position={caps} base"
        f" | max_total_notional={config.risk.max_total_notional or 'not configured'} USDC"
        f" | max_order_notional={config.risk.max_order_notional} USDC"
        f" | max_daily_loss={config.risk.max_daily_loss} USDC"
        f" | book_age={config.risk.stale_book_seconds}s"
        f" | account_age={config.risk.account_age_seconds}s"
    )


async def check_live_exposure(
    source: TradingClient, symbol: str | tuple[str, ...], *, allow_existing_exposure: bool = False
) -> None:
    account = await source.account()
    orders = await source.open_orders()
    symbols = (symbol,) if isinstance(symbol, str) else symbol
    positions = [account.positions.get(key, Decimal(0)) for key in symbols]
    if any(not isinstance(position, Decimal) or not position.is_finite() for position in positions):
        raise RiskViolation("invalid_account")
    occupied = any(position != 0 for position in positions) or any(
        order.symbol in symbols for order in orders
    )
    if occupied and allow_existing_exposure is not True:
        raise LivePreflightRefused("existing_symbol_exposure")
