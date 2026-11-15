"""Deterministic, isolated paper strategy comparisons on one recorded public tape."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import groupby
from pathlib import Path

from .config import Config
from .models import Account
from .paper import PaperExchange
from .presets import strategy_preset
from .public_tape import TapeEvent, read_tape
from .reference import ExternalReference
from .risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation
from .runner import _price_tick
from .strategies import StrategyContext, create_strategy

ZERO = Decimal(0)


@dataclass(frozen=True)
class ComparisonRow:
    strategy: str
    fills: int | None = None
    inventory_min: Decimal | None = None
    inventory_max: Decimal | None = None
    pnl: Decimal | None = None
    max_drawdown: Decimal | None = None
    quote_uptime_pct: Decimal | None = None
    status: str = "unavailable: no recorded HL books"


@dataclass(frozen=True)
class Comparison:
    start_ms: int
    end_ms: int
    rows: tuple[ComparisonRow, ...]


def format_comparison(result: Comparison) -> str:
    lines = [
        "PAPER COMPARISON | simulated seconds=" + str((result.end_ms - result.start_ms) / 1000),
        "strategy | fills | inventory min..max | PnL USDC | max drawdown USDC"
        " | two-sided uptime | status",
    ]
    for row in result.rows:
        if row.fills is None:
            lines.append(f"{row.strategy} | - | - | - | - | - | {row.status}")
        else:
            lines.append(
                f"{row.strategy} | {row.fills} | {row.inventory_min:f}..{row.inventory_max:f}"
                f" | {row.pnl:+.6f} | {row.max_drawdown:.6f}"
                f" | {row.quote_uptime_pct:.2f}% | {row.status}"
            )
    lines.append(
        "Simulated only: approximate queue; excludes fees, funding and liquidation. No live orders."
    )
    return "\n".join(lines)


async def compare_strategies(
    config: Config, tape_path: Path, *, preset: str = "balanced"
) -> Comparison:
    if config.markets:
        raise ValueError("comparison requires a single-symbol config")
    size, events = read_tape(tape_path, config.symbol)
    glyde_index = next(
        i for i, event in enumerate(events) if event.source == "glyde" and event.book
    )
    hl_index = next(
        (i for i, event in enumerate(events) if event.source == "hyperliquid" and event.book), None
    )
    baseline = max(glyde_index, hl_index) if hl_index is not None else glyde_index
    if events[baseline].received_at_ms >= events[-1].received_at_ms:
        raise ValueError("no comparison window after the book baselines")
    rows = []
    for name, strategy, external in (
        ("PMM", "pure_market_making", False),
        ("inventory-skewed", "inventory_skewed", False),
        ("grid", "grid", False),
        ("HL-reference", "pure_market_making", True),
    ):
        if external and hl_index is None:
            rows.append(ComparisonRow(name))
        else:
            rows.append(
                await _simulate(config, size, events, baseline, name, strategy, preset, external)
            )
    return Comparison(events[baseline].received_at_ms, events[-1].received_at_ms, tuple(rows))


async def _simulate(
    config: Config,
    size: int,
    events: list[TapeEvent],
    baseline: int,
    name: str,
    strategy_name: str,
    preset: str,
    external: bool,
) -> ComparisonRow:
    start, end = events[baseline].received_at_ms, events[-1].received_at_ms
    now = start

    class TapePaper(PaperExchange):
        async def account(self) -> Account:
            return replace(await super().account(), received_at=now / 1000)

    paper = TapePaper(config.paper_balance, clock_ms=lambda: now)
    paper.use_trade_tape()
    book = next(
        event.book
        for event in reversed(events[: baseline + 1])
        if event.source == "glyde" and event.book
    )
    assert book is not None and book.mid is not None
    values = strategy_preset(
        strategy_name,
        preset,
        book.mid,
        config.risk.max_order_notional,
        config.risk.max_position,
        reference_source="hyperliquid" if external else "glyde",
    )
    reference = (
        ExternalReference(
            config.symbol,
            max_deviation_bps=Decimal(values["max_reference_deviation_bps"]),
            skew_bps=Decimal(values["reference_skew_bps"]),
            stale_seconds=Decimal(values["reference_stale_seconds"]),
            clock=lambda: now / 1000,
            wall_clock=lambda: now / 1000,
        )
        if external
        else None
    )
    guard = RiskGuard(
        RiskLimits.from_config(config.risk),
        clock=lambda: now / 1000,
        wall_clock=lambda: now / 1000,
        reference_check=reference.center if reference else None,
    )
    managed = RiskManagedClient(paper, guard, cleanup_symbol=config.symbol)
    # Pre-roll seeds original source/arrival cuts and trade dedupe, without placing quotes.
    for event in events[: baseline + 1]:
        if event.book:
            if event.source == "glyde":
                guard.update_book(event.book)
                await paper.on_book(event.book)
            elif reference:
                reference.update(event.book)
        else:
            for trade in event.trades:
                await paper.on_trade(trade)
    strategy = create_strategy(
        strategy_name,
        StrategyContext(
            managed,
            config.symbol,
            _price_tick(book, size),
            Decimal(1).scaleb(-size),
            clock=lambda: now / 1000,
            quote_center=reference.center if reference else None,
        ),
        values,
    )
    await strategy.on_book(book)
    low = high = drawdown = ZERO
    peak = config.paper_balance
    uptime = 0
    prior_time, quoting, running = start, False, True
    status = "completed"
    fill_index = 0

    def advance(timestamp: int) -> None:
        nonlocal now, prior_time, uptime
        if quoting:
            uptime += timestamp - prior_time
        now = prior_time = timestamp

    async def sample() -> Account:
        nonlocal low, high, peak, drawdown, quoting
        account = await paper.account()
        position = account.positions.get(config.symbol, ZERO)
        low, high = min(low, position), max(high, position)
        peak = max(peak, account.equity)
        drawdown = max(drawdown, peak - account.equity)
        quoting = {order.side for order in await paper.open_orders()} == {"buy", "sell"}
        return account

    async def stop(error: RiskViolation) -> None:
        nonlocal running, status
        status = "stopped: " + str(error)
        running = False
        await strategy.stop()
        await managed.close()

    async def tick() -> None:
        if running:
            try:
                guard.update_account(await paper.account())
                guard.check_fresh(config.symbol)
                await strategy.on_tick()
            except RiskViolation as error:
                await stop(error)
        await sample()

    try:
        await tick()
        next_tick = start + 250
        for receipt, group in groupby(
            events[baseline + 1 :], key=lambda event: event.received_at_ms
        ):
            while next_tick < receipt:
                advance(next_tick)
                await tick()
                next_tick += 250
            advance(receipt)
            for event in group:
                if event.book:
                    if event.source == "glyde":
                        await paper.on_book(event.book)
                        if running:
                            guard.update_book(event.book)
                            strategy.context = replace(
                                strategy.context, price_tick=_price_tick(event.book, size)
                            )
                            await strategy.on_book(event.book)
                    elif reference and running:
                        reference.update(event.book)
                elif running:
                    for trade in event.trades:
                        await paper.on_trade(trade)
                        for fill in paper.fills[fill_index:]:
                            await strategy.on_fill(fill)
                        fill_index = len(paper.fills)
                        await sample()
                await sample()
            if next_tick == receipt:
                await tick()
                next_tick += 250
        account = await sample()
    finally:
        try:
            await strategy.stop()
        finally:
            await managed.close()
    return ComparisonRow(
        name,
        len(paper.fills),
        low,
        high,
        account.equity - config.paper_balance,
        drawdown,
        Decimal(uptime) * 100 / Decimal(end - start),
        status,
    )
