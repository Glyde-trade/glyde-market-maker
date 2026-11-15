"""Paper-only replay through the real strategy, simulator and risk wrapper, on a virtual clock."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from .book_history import read_window
from .config import Config
from .models import Account, Book
from .paper import PaperExchange
from .risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation
from .runlog import RunLog
from .runner import _price_tick
from .strategies import StrategyContext, create_strategy
from .strategies.factory import validate_strategy_config


@dataclass(frozen=True)
class ReplayResult:
    start_ms: int
    end_ms: int
    inventory: Decimal
    pnl: Decimal
    fills: int
    risk_stop: str | None


async def replay(
    config: Config, recording: Path, minutes: int, *, state_directory: Path | None = None
) -> ReplayResult:
    if state_directory is None:
        return await _replay(config, recording, minutes)
    with RunLog(state_directory / "logs", mode="paper", strategy=config.strategy) as journal:
        try:
            result = await _replay(config, recording, minutes, journal=journal)
        except BaseException as error:
            journal.emit(
                "run_ended",
                outcome="cancelled"
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt))
                else "error",
            )
            raise
        journal.emit("run_ended", outcome="risk_stop" if result.risk_stop else "completed")
        return result


async def _replay(
    config: Config, recording: Path, minutes: int, *, journal: RunLog | None = None
) -> ReplayResult:
    if config.markets:
        raise ValueError("record/replay requires a single-symbol configuration")
    policy = validate_strategy_config(config.strategy, config.strategy_config)
    if getattr(policy, "ping_pong_enabled", False):
        raise ValueError(
            "ping-pong requires identified tape fills, unavailable in book-only replay"
        )
    size, start, observations = read_window(recording, config.symbol, minutes)
    config = replace(config, mode="paper")
    now = start / 1000

    class ReplayPaper(PaperExchange):
        async def account(self) -> Account:
            return replace(await super().account(), received_at=now)

    paper = ReplayPaper(config.paper_balance)
    guard = RiskGuard(
        RiskLimits.from_config(config.risk),
        clock=lambda: now,
        wall_clock=lambda: now,
    )
    managed = RiskManagedClient(paper, guard)
    first = observations[0].book
    strategy = create_strategy(
        config.strategy,
        StrategyContext(
            managed,
            config.symbol,
            _price_tick(first, size),
            Decimal(1).scaleb(-size),
            clock=lambda: now,
        ),
        config.strategy_config,
    )
    fill_index = 0
    observed = 0
    reason = None
    if journal is not None:
        journal.emit("strategy_started")

    async def observe(book: Book) -> None:
        nonlocal fill_index, observed
        observed += 1
        guard.update_book(book)
        await paper.on_book(book)
        for fill in paper.fills[fill_index:]:
            await strategy.on_fill(fill)
        fill_index = len(paper.fills)
        strategy.context = replace(strategy.context, price_tick=_price_tick(book, size))
        await strategy.on_book(book)

    async def tick() -> None:
        guard.update_account(await paper.account())
        guard.check_fresh(config.symbol)
        await strategy.on_tick()

    try:
        await observe(first)
        await tick()
        next_tick = start + 250
        for observation in observations[1:]:
            receipt = observation.received_at_ms
            while next_tick < receipt:
                now = next_tick / 1000
                await tick()
                next_tick += 250
            now = receipt / 1000
            await observe(observation.book)
            if next_tick == receipt:
                await tick()
                next_tick += 250
    except RiskViolation as error:
        reason = str(error)
    finally:
        try:
            await strategy.stop()
        finally:
            await managed.close()
    account = await paper.account()
    if journal is not None:
        journal.emit(
            "progress",
            book_messages=observed,
            pending_orders=len(await paper.open_orders()),
            paper_fills=len(paper.fills),
        )
    return ReplayResult(
        start,
        int(now * 1000),
        account.positions.get(config.symbol, Decimal(0)),
        account.equity - config.paper_balance,
        len(paper.fills),
        reason,
    )
