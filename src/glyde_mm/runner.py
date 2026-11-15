"""One paper/live lifecycle: shared strategy, risk boundary and bounded book stream."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from .client import OneApiClient, OneApiError, book_from_wire
from .config import Config
from .models import Book
from .paper import PaperExchange
from .risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation
from .strategies import StrategyContext, create_strategy


def risk_state_path(config: Config, directory: Path) -> Path:
    identity = "|".join((config.mode, config.base_url, config.owner, config.vault_address or ""))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return directory / f"{config.mode}-{digest}.json"


def _price_tick(book: Book, size_decimals: int) -> Decimal:
    mid = book.mid
    if mid is None or not mid.is_finite() or mid <= 0:
        raise RiskViolation("invalid_book")
    # Perpetual prices use at most five significant figures and 6-szDecimals places.
    return max(Decimal(1).scaleb(mid.adjusted() - 4), Decimal(1).scaleb(-max(0, 6 - size_decimals)))


async def run(
    config: Config,
    *,
    state_directory: Path = Path(".glyde-mm"),
    client_factory: Callable = OneApiClient,
    paper_factory: Callable = PaperExchange,
    strategy_factory: Callable = create_strategy,
    stop_event: asyncio.Event | None = None,
    report: Callable[[str], object] = print,
) -> None:
    if config.mode == "live":
        if not all(
            re.fullmatch(r"0x[0-9a-fA-F]{40}", value or "")
            for value in (config.owner, config.vault_address)
        ):
            raise ValueError("live mode requires registered account and vault addresses")
        config.secret(config.private_key_env)
        config.secret(config.read_api_key_env)
    limits = RiskLimits(
        max_position=config.risk.max_position,
        max_order_notional=config.risk.max_order_notional,
        max_daily_loss=config.risk.max_daily_loss,
        stale_book_seconds=float(config.risk.stale_book_seconds),
        account_age_seconds=float(config.risk.account_age_seconds),
    )
    guard = RiskGuard(limits, state_path=risk_state_path(config, state_directory))
    stop = stop_event or asyncio.Event()
    async with client_factory(config) as source:
        raw = paper_factory(config.paper_balance) if config.mode == "paper" else source
        managed = RiskManagedClient(raw, guard)
        strategy = None
        tasks: list[asyncio.Task] = []
        try:
            metadata = await source.meta()
            market = next(
                (row for row in metadata["universe"] if row["name"] == config.symbol), None
            )
            if (
                market is None
                or type(market["szDecimals"]) is not int
                or not 0 <= market["szDecimals"] <= 6
            ):
                raise ValueError("market size precision is unavailable")
            size_decimals = market["szDecimals"]
            stream = source.stream([{"type": "l2Book", "coin": config.symbol}])
            async with asyncio.timeout(10):
                while True:
                    frame = await anext(stream)
                    if frame.get("channel") == "l2Book":
                        initial = book_from_wire(frame["data"])
                        if initial.symbol != config.symbol:
                            raise OneApiError("book market mismatch")
                        break
            guard.update_book(initial)
            if config.mode == "paper":
                await raw.on_book(initial)
            guard.update_account(await raw.account())
            guard.check_fresh(config.symbol)
            context = StrategyContext(
                managed,
                config.symbol,
                _price_tick(initial, size_decimals),
                Decimal(1).scaleb(-size_decimals),
            )
            strategy = strategy_factory(config.strategy, context, config.strategy_config)
            await strategy.on_book(initial)
            report(f"{config.mode.upper()} {config.symbol}: strategy started with risk limits")
            fill_index = 0

            async def receive_books() -> None:
                nonlocal fill_index
                async for frame in stream:
                    if frame.get("channel") != "l2Book":
                        continue
                    book = book_from_wire(frame["data"])
                    if book.symbol != config.symbol:
                        raise OneApiError("book market mismatch")
                    guard.update_book(book)
                    if config.mode == "paper":
                        await raw.on_book(book)
                        for fill in raw.fills[fill_index:]:
                            await strategy.on_fill(fill)
                        fill_index = len(raw.fills)
                    strategy.context = StrategyContext(
                        managed,
                        config.symbol,
                        _price_tick(book, size_decimals),
                        context.quantity_step,
                        context.min_notional,
                    )
                    await strategy.on_book(book)
                raise OneApiError("book stream disconnected")

            async def drive() -> None:
                refreshed = 0.0
                while True:
                    now = time.monotonic()
                    if now - refreshed >= min(2.0, limits.account_age_seconds / 2):
                        guard.update_account(await raw.account())
                        refreshed = now
                    await strategy.on_tick()
                    await asyncio.sleep(0.25)

            async def watchdog() -> None:
                while True:
                    await managed.check_liveness()
                    await asyncio.sleep(0.25)

            tasks = [
                asyncio.create_task(receive_books()),
                asyncio.create_task(drive()),
                asyncio.create_task(watchdog()),
                asyncio.create_task(stop.wait()),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not tasks[-1]:
                    task.result()
                    raise OneApiError("strategy task exited unexpectedly")
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if strategy is not None:
                    await strategy.stop()
            finally:
                await managed.close()
