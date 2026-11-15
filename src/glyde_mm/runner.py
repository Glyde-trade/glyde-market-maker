"""One paper/live lifecycle: shared strategy, risk boundary and bounded book stream."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from .client import OneApiClient, OneApiError, book_from_wire
from .config import Config
from .models import Book
from .paper import PaperExchange
from .preflight import check_live_exposure
from .reference import HYPERLIQUID_PUBLIC_URL, ExternalReference
from .risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation
from .runlog import RunLog
from .shutdown import RunnerControl
from .strategies import StrategyContext, create_strategy
from .strategies.factory import validate_strategy_config


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


async def report_paper_status(
    paper: PaperExchange,
    config: Config,
    report: Callable[[str], object],
    *,
    summary: bool = False,
    risk_stop: str | None = None,
) -> None:
    account = await paper.account()
    orders = await paper.open_orders()
    position = account.positions.get(config.symbol, Decimal(0))
    pnl = account.equity - config.paper_balance
    fills = sum(fill.symbol == config.symbol for fill in paper.fills)
    quotes = sum(order.symbol == config.symbol for order in orders)
    label = "PAPER SUMMARY" if summary else "PAPER"
    report(
        f"{label} {config.symbol} | inventory={position:f} | simulated_pnl={pnl:+f} USDC"
        f" | fills={fills} | open_quotes={quotes} | risk_stop={risk_stop or 'none'}"
    )


async def run(
    config: Config,
    *,
    state_directory: Path = Path(".glyde-mm"),
    client_factory: Callable = OneApiClient,
    reference_factory: Callable = OneApiClient,
    paper_factory: Callable = PaperExchange,
    strategy_factory: Callable = create_strategy,
    stop_event: asyncio.Event | None = None,
    report: Callable[[str], object] = print,
    allow_existing_exposure: bool = False,
) -> None:
    stop = stop_event or asyncio.Event()
    with RunLog(state_directory / "logs", mode=config.mode, strategy=config.strategy) as journal:
        report(f"GLYDE-MM {journal.identity['version']} commit={journal.identity['commit']}")
        outcome = "completed"
        try:
            async with RunnerControl(config, state_directory, stop):
                await _run_session(
                    config,
                    state_directory=state_directory,
                    client_factory=client_factory,
                    reference_factory=reference_factory,
                    paper_factory=paper_factory,
                    strategy_factory=strategy_factory,
                    stop_event=stop,
                    report=report,
                    journal=journal,
                    allow_existing_exposure=allow_existing_exposure,
                )
            if stop.is_set():
                outcome = "stopped"
        except BaseException as error:
            outcome = (
                "cancelled"
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt))
                else "risk_stop"
                if isinstance(error, RiskViolation)
                else "transport_error"
                if isinstance(error, OneApiError)
                else "error"
            )
            raise
        finally:
            journal.emit("run_ended", outcome=outcome)


async def _run_session(
    config: Config,
    *,
    state_directory: Path = Path(".glyde-mm"),
    client_factory: Callable = OneApiClient,
    reference_factory: Callable = OneApiClient,
    paper_factory: Callable = PaperExchange,
    strategy_factory: Callable = create_strategy,
    stop_event: asyncio.Event | None = None,
    report: Callable[[str], object] = print,
    journal: RunLog,
    allow_existing_exposure: bool = False,
) -> None:
    policy = validate_strategy_config(config.strategy, config.strategy_config)
    reference = None
    if getattr(policy, "reference_source", "glyde") == "hyperliquid":
        reference = ExternalReference(
            config.symbol,
            max_deviation_bps=policy.max_reference_deviation_bps,
            skew_bps=policy.reference_skew_bps,
            stale_seconds=policy.reference_stale_seconds,
        )
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
    guard = RiskGuard(
        limits,
        state_path=risk_state_path(config, state_directory),
        reference_check=reference.center if reference else None,
    )
    stop = stop_event or asyncio.Event()
    async with client_factory(config) as source:
        if config.mode == "live":
            if stop.is_set():
                return
            # Refusal must not enter managed cleanup, which can cancel existing orders.
            await check_live_exposure(
                source, config.symbol, allow_existing_exposure=allow_existing_exposure
            )
            if stop.is_set():
                return
        raw = paper_factory(config.paper_balance) if config.mode == "paper" else source
        managed = RiskManagedClient(raw, guard, stop_event=stop, cleanup_symbol=config.symbol)
        strategy = None
        stream = None
        reference_stream = None
        resources = AsyncExitStack()
        tasks: list[asyncio.Task] = []
        risk_stop = None
        try:
            if stop.is_set():
                return
            metadata = await source.meta()
            if stop.is_set():
                return
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
            resources.push_async_callback(stream.aclose)
            async with asyncio.timeout(10):
                while True:
                    frame = await anext(stream)
                    if stop.is_set():
                        return
                    if frame.get("channel") == "l2Book":
                        initial = book_from_wire(frame["data"])
                        if initial.symbol != config.symbol:
                            raise OneApiError("book market mismatch")
                        break
            if reference is not None:
                public_config = replace(
                    config,
                    mode="paper",
                    base_url=HYPERLIQUID_PUBLIC_URL,
                    owner="",
                    vault_address=None,
                )
                reference_client = reference_factory(public_config)
                resources.push_async_callback(reference_client.close)
                reference_stream = reference_client.stream(
                    [{"type": "l2Book", "coin": config.symbol}]
                )
                resources.push_async_callback(reference_stream.aclose)
                async with asyncio.timeout(10):
                    try:
                        while True:
                            frame = await anext(reference_stream)
                            if stop.is_set():
                                return
                            if frame.get("channel") == "l2Book":
                                reference.update(book_from_wire(frame["data"]))
                                break
                    except StopAsyncIteration:
                        raise RiskViolation("reference_disconnected") from None
            guard.update_book(initial)
            if config.mode == "paper":
                await raw.on_book(initial)
            guard.update_account(await raw.account())
            if stop.is_set():
                return
            guard.check_fresh(config.symbol)
            context = StrategyContext(
                managed,
                config.symbol,
                _price_tick(initial, size_decimals),
                Decimal(1).scaleb(-size_decimals),
                quote_center=reference.center if reference else None,
            )
            strategy = strategy_factory(config.strategy, context, config.strategy_config)
            await strategy.on_book(initial)
            report(f"{config.mode.upper()} {config.symbol}: strategy started with risk limits")
            journal.emit("strategy_started")
            fill_index = 0
            book_messages = 1

            async def receive_books() -> None:
                nonlocal fill_index, book_messages
                async for frame in stream:
                    if frame.get("channel") != "l2Book":
                        continue
                    book = book_from_wire(frame["data"])
                    if book.symbol != config.symbol:
                        raise OneApiError("book market mismatch")
                    guard.update_book(book)
                    if reference is not None:
                        guard.check_fresh(config.symbol)
                    book_messages += 1
                    if config.mode == "paper":
                        await raw.on_book(book)
                        for fill in raw.fills[fill_index:]:
                            await strategy.on_fill(fill)
                        fill_index = len(raw.fills)
                    strategy.context = replace(
                        strategy.context, price_tick=_price_tick(book, size_decimals)
                    )
                    await strategy.on_book(book)
                raise OneApiError("book stream disconnected")

            async def receive_reference() -> None:
                assert reference_stream is not None and reference is not None
                async for frame in reference_stream:
                    if frame.get("channel") == "l2Book":
                        reference.update(book_from_wire(frame["data"]))
                        guard.check_fresh(config.symbol)
                raise RiskViolation("reference_disconnected")

            async def drive() -> None:
                refreshed = 0.0
                reported = None
                logged = None
                while not stop.is_set():
                    now = time.monotonic()
                    if now - refreshed >= min(2.0, limits.account_age_seconds / 2):
                        guard.update_account(await raw.account())
                        refreshed = now
                    if stop.is_set():
                        return
                    await strategy.on_tick()
                    if logged is None or now - logged >= 2:
                        journal.emit(
                            "progress",
                            book_messages=book_messages,
                            pending_orders=len(guard.pending),
                            paper_fills=len(raw.fills) if config.mode == "paper" else 0,
                        )
                        logged = now
                    if config.mode == "paper" and (reported is None or now - reported >= 2):
                        await report_paper_status(raw, config, report, risk_stop=guard.halted)
                        reported = now
                    await asyncio.sleep(0.25)

            async def watchdog() -> None:
                while True:
                    if reference is not None:
                        guard.check_fresh(config.symbol)
                    await managed.check_liveness()
                    await asyncio.sleep(0.25)

            tasks = [
                asyncio.create_task(receive_books()),
                asyncio.create_task(drive()),
                asyncio.create_task(watchdog()),
                asyncio.create_task(stop.wait()),
            ]
            if reference is not None:
                tasks.insert(0, asyncio.create_task(receive_reference()))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not tasks[-1]:
                    try:
                        task.result()
                    except RiskViolation as error:
                        if not stop.is_set() or str(error) != "client_closed":
                            raise
                    if not stop.is_set():
                        raise OneApiError("strategy task exited unexpectedly")
        except RiskViolation as error:
            risk_stop = str(error)
            raise
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await resources.aclose()
            finally:
                try:
                    if strategy is not None:
                        await strategy.stop()
                finally:
                    try:
                        await managed.close()
                    finally:
                        if config.mode == "paper":
                            reason = risk_stop or guard.halted
                            await report_paper_status(
                                raw,
                                config,
                                report,
                                summary=True,
                                risk_stop=None if reason == "closed" else reason,
                            )
