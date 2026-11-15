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

from .alerts import notify_session
from .client import OneApiClient, OneApiError, book_from_wire, trades_from_wire
from .config import Config
from .dashboard import SnapshotPublisher
from .live_state import LiveStateClient
from .metrics import Metrics
from .models import Book
from .paper import PaperExchange
from .preflight import check_live_exposure
from .reference import HYPERLIQUID_PUBLIC_URL, ExternalReference
from .risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation, StaleMarket
from .runlog import RunLog
from .shutdown import RunnerControl
from .strategies import StrategyContext, create_strategy
from .strategies.avellaneda_market_making import AvellanedaConfig, AvellanedaMarketMakingStrategy
from .strategies.factory import Strategy, validate_strategy_config
from .strategies.pure_market_making import PureMarketMakingConfig
from .transport import ClientFactory, JsonObject


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
    client_factory: ClientFactory = OneApiClient,
    reference_factory: ClientFactory = OneApiClient,
    paper_factory: Callable[[Decimal], PaperExchange] = PaperExchange,
    strategy_factory: Callable[[str, StrategyContext, JsonObject], Strategy] = create_strategy,
    stop_event: asyncio.Event | None = None,
    report: Callable[[str], object] = print,
    allow_existing_exposure: bool = False,
    log_directory: Path | None = None,
) -> None:
    stop = stop_event or asyncio.Event()
    with (
        RunLog(
            log_directory if log_directory is not None else state_directory / "logs",
            mode=config.mode,
            strategy=config.strategy,
        ) as journal,
        Metrics(config) as metrics,
    ):
        report(f"GLYDE-MM {journal.identity['version']} commit={journal.identity['commit']}")
        outcome = "completed"
        failure: BaseException | None = None
        risk_reasons: list[str] = []
        observer = SnapshotPublisher(journal.directory / "dashboard.json", config, journal.run_id)
        report(f"Dashboard state: {observer.path}")
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
                    observer=observer,
                    allow_existing_exposure=allow_existing_exposure,
                    risk_reasons=risk_reasons,
                    metrics=metrics,
                )
            if stop.is_set():
                outcome = "stopped"
        except BaseException as error:
            failure = error
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
            observer.finish(outcome)
            journal.emit("run_ended", outcome=outcome)
            await notify_session(
                config, journal.run_id, outcome, failure, report=report, risk_reasons=risk_reasons
            )


async def _run_session(
    config: Config,
    *,
    state_directory: Path = Path(".glyde-mm"),
    client_factory: ClientFactory = OneApiClient,
    reference_factory: ClientFactory = OneApiClient,
    paper_factory: Callable[[Decimal], PaperExchange] = PaperExchange,
    strategy_factory: Callable[[str, StrategyContext, JsonObject], Strategy] = create_strategy,
    stop_event: asyncio.Event | None = None,
    report: Callable[[str], object] = print,
    journal: RunLog,
    observer: SnapshotPublisher,
    risk_reasons: list[str],
    metrics: Metrics,
    allow_existing_exposure: bool = False,
) -> None:
    selected = {market.symbol: market for market in config.market_configs()}
    references = {}
    for symbol, market_config in selected.items():
        policy = validate_strategy_config(market_config.strategy, market_config.strategy_config)
        if (
            config.mode == "live"
            and isinstance(policy, AvellanedaConfig)
            and policy.volatility_source == "book_and_trades"
        ):
            raise ValueError("book_and_trades volatility requires streaming paper mode")
        if config.mode == "live" and getattr(policy, "filled_order_delay", 0) > 0:
            raise ValueError("filled_order_delay requires identified paper-stream fills")
        if config.mode == "live" and getattr(policy, "ping_pong_enabled", False):
            raise ValueError(
                "ping-pong requires paper tape fills until live fill delivery is qualified"
            )
        if isinstance(policy, PureMarketMakingConfig) and policy.reference_source == "hyperliquid":
            references[symbol] = ExternalReference(
                symbol,
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
        max_total_notional=config.risk.max_total_notional,
        max_daily_loss=config.risk.max_daily_loss,
        max_orders_per_minute=config.risk.max_orders_per_minute,
        stale_book_seconds=float(config.risk.stale_book_seconds),
        account_age_seconds=float(config.risk.account_age_seconds),
    )

    def check_reference(book: Book) -> Decimal:
        if book.symbol in references:
            return references[book.symbol].center(book)
        assert book.mid is not None
        return book.mid

    guard = RiskGuard(
        limits,
        state_path=risk_state_path(config, state_directory),
        position_limits={s: c.risk.max_position for s, c in selected.items()},
        reference_check=check_reference if references else None,
    )
    stop = stop_event or asyncio.Event()
    async with client_factory(config) as source:
        if config.mode == "live":
            if stop.is_set():
                return
            # All selected markets must pass before cleanup gains cancellation authority.
            await check_live_exposure(
                source, config.symbols, allow_existing_exposure=allow_existing_exposure
            )
            if stop.is_set():
                return
        paper = paper_factory(config.paper_balance) if config.mode == "paper" else None
        live = LiveStateClient(source, config, metrics=metrics) if paper is None else None
        raw = paper if paper is not None else live
        assert raw is not None
        cleanup_allowed = paper is not None
        if paper is not None:
            paper.use_trade_tape()
        managed = RiskManagedClient(
            raw,
            guard,
            stop_event=stop,
            cleanup_symbols=config.symbols,
            metrics=metrics,
            isolate_stale_markets=len(selected) > 1,
        )
        strategies: dict[str, Strategy] = {}
        stream = reference_stream = None
        resources = AsyncExitStack()
        tasks: list[asyncio.Task[None] | asyncio.Task[bool]] = []
        risk_stop = None
        try:
            if stop.is_set():
                return
            metadata = await source.meta()
            if stop.is_set():
                return
            precision = {}
            for symbol in selected:
                market = next((row for row in metadata["universe"] if row["name"] == symbol), None)
                if (
                    market is None
                    or type(market["szDecimals"]) is not int
                    or not 0 <= market["szDecimals"] <= 6
                ):
                    raise ValueError("market size precision is unavailable")
                precision[symbol] = market["szDecimals"]
            subscriptions = [{"type": "l2Book", "coin": symbol} for symbol in selected]
            if paper is not None:
                subscriptions += [{"type": "trades", "coin": symbol} for symbol in selected]
            stream = live.stream() if live is not None else source.stream(subscriptions)
            resources.push_async_callback(stream.aclose)
            initial_books: dict[str, Book] = {}
            async with asyncio.timeout(10):
                while initial_books.keys() != selected.keys():
                    frame = await anext(stream)
                    if stop.is_set():
                        return
                    if frame.get("channel") == "l2Book":
                        book = book_from_wire(frame["data"])
                        if book.symbol not in selected:
                            raise OneApiError("book market mismatch")
                        initial_books[book.symbol] = book
            metrics.connected()
            if references:
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
                    [{"type": "l2Book", "coin": symbol} for symbol in references]
                )
                resources.push_async_callback(reference_stream.aclose)
                received: set[str] = set()
                async with asyncio.timeout(10):
                    try:
                        while received != references.keys():
                            frame = await anext(reference_stream)
                            if stop.is_set():
                                return
                            if frame.get("channel") == "l2Book":
                                book = book_from_wire(frame["data"])
                                if book.symbol not in references:
                                    raise RiskViolation("reference_market_mismatch")
                                references[book.symbol].update(book)
                                received.add(book.symbol)
                    except StopAsyncIteration:
                        raise RiskViolation("reference_disconnected") from None
            for book in initial_books.values():
                guard.update_book(book)
                metrics.book(guard.books[book.symbol])
                if paper is not None:
                    await paper.on_book(book)
            if live is not None:
                await check_live_exposure(
                    live, config.symbols, allow_existing_exposure=allow_existing_exposure
                )
                cleanup_allowed = True
            guard.update_account(await raw.account())
            if stop.is_set():
                return
            for symbol, market_config in selected.items():
                initial = guard.check_fresh(symbol)
                context = StrategyContext(
                    managed,
                    symbol,
                    _price_tick(initial, precision[symbol]),
                    Decimal(1).scaleb(-precision[symbol]),
                    quote_center=references[symbol].center if symbol in references else None,
                )
                strategy = strategy_factory(
                    market_config.strategy, context, market_config.strategy_config
                )
                strategies[symbol] = strategy
                await strategy.on_book(initial)
                report(
                    f"{config.mode.upper()} {symbol}: strategy started "
                    "with shared account risk limits"
                )
                journal.emit("strategy_started")
            market_lock = asyncio.Lock()
            paused: dict[str, str] = {}

            async def pause_market(symbol: str, dependency: str) -> None:
                if symbol not in paused:
                    await strategies[symbol].stop()
                    report(f"{config.mode.upper()} {symbol}: quoting paused (stale {dependency})")
                paused[symbol] = dependency

            async def refresh_markets() -> None:
                for symbol in selected:
                    try:
                        book = guard.check_fresh(symbol)
                        guard.check_fresh(paused.get(symbol, symbol))
                    except StaleMarket as error:
                        await pause_market(symbol, error.symbol)
                        continue
                    if symbol in paused:
                        policy = selected[symbol]
                        context = StrategyContext(
                            managed,
                            symbol,
                            _price_tick(book, precision[symbol]),
                            Decimal(1).scaleb(-precision[symbol]),
                            quote_center=references[symbol].center
                            if symbol in references
                            else None,
                        )
                        strategies[symbol] = strategy_factory(
                            policy.strategy, context, policy.strategy_config
                        )
                        await strategies[symbol].on_book(book)
                        del paused[symbol]
                        report(f"{config.mode.upper()} {symbol}: quoting resumed")

            fill_index = 0
            book_messages = len(initial_books)

            async def deliver_fills() -> None:
                nonlocal fill_index
                assert paper is not None
                for fill in paper.fills[fill_index:]:
                    await strategies[fill.symbol].on_fill(fill)
                fill_index = len(paper.fills)
                metrics.paper_fills(paper)

            async def receive_books() -> None:
                nonlocal book_messages
                async for frame in stream:
                    if live is not None and frame.get("channel") == "mmOwner":
                        await managed.refresh_account()
                        for fill in frame["fills"]:
                            metrics.fill(fill.symbol)
                            await strategies[fill.symbol].on_fill(fill)
                        continue
                    if paper is not None and frame.get("channel") == "trades":
                        trades = trades_from_wire(frame["data"])
                        if any(trade.symbol not in selected for trade in trades):
                            raise OneApiError("trade market mismatch")
                        for trade in trades:
                            strategy = strategies[trade.symbol]
                            if isinstance(strategy, AvellanedaMarketMakingStrategy):
                                await strategy.on_trade(trade)
                            await paper.on_trade(trade)
                        await deliver_fills()
                        guard.update_account(await raw.account())
                        continue
                    if frame.get("channel") != "l2Book":
                        continue
                    book = book_from_wire(frame["data"])
                    if book.symbol not in selected:
                        raise OneApiError("book market mismatch")
                    guard.update_book(book)
                    metrics.book(guard.books[book.symbol])
                    if book.symbol in references:
                        guard.check_fresh(book.symbol)
                    book_messages += 1
                    if paper is not None:
                        await paper.on_book(book)
                        await deliver_fills()
                    strategy = strategies[book.symbol]
                    strategy.context = replace(
                        strategy.context, price_tick=_price_tick(book, precision[book.symbol])
                    )
                    await strategy.on_book(book)
                raise OneApiError("book stream disconnected")

            async def receive_reference() -> None:
                assert reference_stream is not None
                async for frame in reference_stream:
                    if frame.get("channel") == "l2Book":
                        book = book_from_wire(frame["data"])
                        if book.symbol not in references:
                            raise RiskViolation("reference_market_mismatch")
                        references[book.symbol].update(book)
                        guard.check_fresh(book.symbol)
                raise RiskViolation("reference_disconnected")

            async def drive() -> None:
                refreshed = 0.0
                reported = logged = None
                while not stop.is_set():
                    now = time.monotonic()
                    if paper is not None and now - refreshed >= min(
                        2.0, limits.account_age_seconds / 2
                    ):
                        guard.update_account(await raw.account())
                        refreshed = now
                    async with market_lock:
                        if len(selected) > 1:
                            await refresh_markets()
                        for symbol, strategy in strategies.items():
                            if stop.is_set():
                                return
                            if symbol in paused:
                                continue
                            try:
                                await strategy.on_tick()
                            except StaleMarket as error:
                                if len(selected) == 1 or error.symbol not in selected:
                                    raise
                                await pause_market(error.symbol, error.symbol)
                                await pause_market(symbol, error.symbol)
                            if paper is not None:
                                metrics.paper_fills(paper)
                    if logged is None or now - logged >= 2:
                        journal.emit(
                            "progress",
                            book_messages=book_messages,
                            pending_orders=len(guard.pending),
                            paper_fills=len(paper.fills) if paper is not None else 0,
                        )
                        await observer.publish(guard, paper=paper)
                        metrics.snapshot(
                            guard.account_state,
                            guard._equity_start,
                            limits.max_daily_loss,
                            paper=paper,
                            initial_balance=config.paper_balance,
                        )
                        logged = now
                    if paper is not None and (reported is None or now - reported >= 2):
                        for market_config in selected.values():
                            await report_paper_status(
                                paper, market_config, report, risk_stop=guard.halted
                            )
                        reported = now
                    await asyncio.sleep(0.25)

            async def watchdog() -> None:
                while True:
                    if live is not None:
                        live.check()
                    async with market_lock:
                        if len(selected) > 1:
                            await refresh_markets()
                        else:
                            guard.check_fresh(config.symbols[0])
                        await managed.check_liveness()
                    await asyncio.sleep(0.25)

            tasks = [
                asyncio.create_task(receive_books()),
                asyncio.create_task(drive()),
                asyncio.create_task(watchdog()),
                asyncio.create_task(stop.wait()),
            ]
            if references:
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
        except BaseException as error:
            if isinstance(error, RiskViolation):
                risk_stop = str(error)
            if guard.halted is not None:
                risk_reasons.append(guard.halted)
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
                    failure = None
                    for strategy in strategies.values():
                        try:
                            await strategy.stop()
                        except Exception as error:
                            failure = failure or error
                    if failure is not None:
                        raise failure
                finally:
                    try:
                        if cleanup_allowed:
                            await managed.close()
                    finally:
                        if live is not None:
                            await live.close()
                        if paper is not None:
                            await observer.publish(guard, paper=paper)
                            reason = risk_stop or guard.halted
                            for market_config in selected.values():
                                await report_paper_status(
                                    paper,
                                    market_config,
                                    report,
                                    summary=True,
                                    risk_stop=None if reason == "closed" else reason,
                                )
