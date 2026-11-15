"""Retail startup summary and a read-only existing-exposure refusal."""

from __future__ import annotations

from decimal import Decimal

from .config import Config
from .models import TradingClient
from .risk import RiskViolation


class LivePreflightRefused(RiskViolation):
    """Existing symbol exposure needs explicit consent before managed cleanup exists."""


def summary(config: Config) -> str:
    return (
        f"PRE-FLIGHT | mode={config.mode.upper()} | symbol={config.symbol}"
        f" | max_position={config.risk.max_position} base"
        f" | max_order_notional={config.risk.max_order_notional} USDC"
        f" | max_daily_loss={config.risk.max_daily_loss} USDC"
        f" | book_age={config.risk.stale_book_seconds}s"
        f" | account_age={config.risk.account_age_seconds}s"
    )


async def check_live_exposure(
    source: TradingClient, symbol: str, *, allow_existing_exposure: bool = False
) -> None:
    account = await source.account()
    orders = await source.open_orders()
    position = account.positions.get(symbol, Decimal(0))
    if not isinstance(position, Decimal) or not position.is_finite():
        raise RiskViolation("invalid_account")
    occupied = position != 0 or any(order.symbol == symbol for order in orders)
    if occupied and allow_existing_exposure is not True:
        raise LivePreflightRefused("existing_symbol_exposure")
