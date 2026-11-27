"""One paper/live lifecycle: shared strategy, risk boundary and bounded book stream."""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import socket
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from .alerts import notify_session
from .client import (
    OneApiClient,
    OneApiError,
    OneApiHTTPError,
    StreamDisconnected,
    SubscriptionDeferred,
    book_from_wire,
    engine_unavailable,
    reconnectable,
    trades_from_wire,
)
from .config import Config
from .dashboard import SnapshotPublisher
from .live_state import LiveStateClient, StreamFault
from .market_preflight import require_preflight
from .metrics import Metrics
from .models import Book, CancelRequest
from .paper import PaperExchange
from .paper_result import PaperResult
from .preflight import check_live_exposure
from .reference import HYPERLIQUID_PUBLIC_URL, ExternalReference
from .risk import (
    RiskGuard,
    RiskLimits,
    RiskManagedClient,
    RiskViolation,
    StaleMarket,
    VenueSweepDeferred,
    venue_proves_closed,
)
from .runlog import RunLog
from .shutdown import RunnerControl
from .strategies import StrategyContext, create_strategy
from .strategies.avellaneda_market_making import AvellanedaConfig, AvellanedaMarketMakingStrategy
from .strategies.factory import Strategy, validate_strategy_config
from .strategies.pure_market_making import PureMarketMakingConfig
from .transport import ClientFactory, JsonObject, MarketClient

RECONNECT_INITIAL_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 30.0
WATCHDOG_PING_SECONDS = 5.0


async def systemd_heartbeat() -> None:
    """Ping systemd's WatchdogSec= while the event loop runs; a wedged process is restarted."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    while True:
        with suppress(OSError), socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
            notify.sendto(b"WATCHDOG=1", address)
        await asyncio.sleep(WATCHDOG_PING_SECONDS)


_T = TypeVar("_T")


def _retry_floor(error: BaseException) -> float:
    return error.retry_after if isinstance(error, SubscriptionDeferred) else 0.0


def _failure_label(error: BaseException) -> str:
    # reasonCode is a sanitized constant; raw frames are never logged.
    if isinstance(error, SubscriptionDeferred):
        return f"{error.reason_code}, retry after {error.retry_after:g}s"
    if isinstance(error, OneApiError):
        # One API errors carry sanitized, credential-free messages by contract.
        return f"{type(error).__name__}: {error}"
    return type(error).__name__


async def _stopped_within(stop: asyncio.Event, seconds: float) -> bool:
    try:
        async with asyncio.timeout(seconds):
            await stop.wait()
    except TimeoutError:
        return False
    return True


async def _unless_stopped(stop: asyncio.Event, work: Coroutine[Any, Any, _T]) -> _T | None:
    """Await work unless stop is set first; abandoned work is cancelled and yields None."""
    task = asyncio.create_task(work)
    waiter = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
        task.cancel()
        await asyncio.gather(task, waiter, return_exceptions=True)
    return None if task.cancelled() else task.result()


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


async def _wait_out_halt(
    source: MarketClient,
    stop: asyncio.Event,
    report: Callable[[str], object],
    max_halt: float,
    work: Callable[[], Awaitable[_T]],
) -> _T | None:
    """Retry a startup read Glyde refuses while it reports its engine halted.

    Bounded by the halt budget; any other failure, or a refusal while Glyde reports itself
    ready, is raised at once. Returns None if stopped while waiting.
    """
    deadline, delay, reported = time.monotonic() + max_halt, RECONNECT_INITIAL_SECONDS, False
    while True:
        try:
            return await work()
        except OneApiHTTPError as error:
            probe = getattr(source, "engine_ready", None)
            ready = await probe() if probe is not None else None
            halted = engine_unavailable(error) if ready is None else not ready
            if not halted or time.monotonic() >= deadline:
                raise
            if not reported:
                report("Glyde reports its engine halted; waiting for it before starting")
                reported = True
        if await _stopped_within(stop, delay):
            return None
        delay = min(delay * 2, RECONNECT_MAX_SECONDS)


async def _resolve_cancel_latch(
    source: MarketClient, guard: RiskGuard, report: Callable[[str], object]
) -> None:
    """Clear a prior run's cancel latch when the venue proves every original terminal."""
    if guard.halted != "cancel_outcome_unknown":
        return
    try:
        open_orders = await source.open_orders()
        history = await source.historical_orders()
    except OneApiError as error:
        reason = type(error).__name__
        report(f"LIVE: cancel_outcome_unknown kept; order history unavailable ({reason})")
        return
    if guard.resolve_cancel_latch(open_orders, history):
        await guard.flush_state()
        report("LIVE: cancel_outcome_unknown resolved; order history shows every original closed")
    else:
        report("LIVE: cancel_outcome_unknown kept; order history lacks proof for an original")


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
    paper_result: PaperResult | None = None,
) -> None:
    if paper_result is not None and config.mode != "paper":
        raise ValueError("paper result requires paper mode")
    stop = stop_event or asyncio.Event()
    with (
        RunLog(
            log_directory if log_directory is not None else state_directory / "logs",
            mode=config.mode,
            strategy=config.strategy,
        ) as journal,
        Metrics(config, collect=True) if paper_result is not None else Metrics(config) as metrics,
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
                    paper_result=paper_result,
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
            if paper_result is not None:
                paper_result.outcome = outcome
            observer.finish(outcome)
            if isinstance(failure, StreamFault):
                journal.emit("stream_fault", **failure.fields())
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
    paper_result: PaperResult | None,
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
        require_preflight(config, state_directory)
        config.secret(config.private_key_env)
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
        simulated=config.mode == "paper",
    )
    stop = stop_event or asyncio.Event()
    async with client_factory(config) as source:
        if config.mode == "live":
            if stop.is_set():
                return
            max_halt = float(config.risk.max_engine_halt_seconds)

            async def startup_reads() -> bool:
                await _resolve_cancel_latch(source, guard, report)
                # All selected markets must pass before cleanup gains cancellation authority.
                await check_live_exposure(
                    source, config.symbols, allow_existing_exposure=allow_existing_exposure
                )
                return True

            if not await _wait_out_halt(source, stop, report, max_halt, startup_reads):
                return
            if stop.is_set():
                return
        paper = paper_factory(config.paper_balance) if config.mode == "paper" else None
        live = LiveStateClient(source, config, metrics=metrics) if paper is None else None
        raw: PaperExchange | LiveStateClient
        if paper is not None:
            raw = paper
        else:
            assert live is not None
            raw = live
        cleanup_allowed = paper is not None
        if paper is not None:
            paper.use_trade_tape()

        async def prove_closed(requests: Sequence[CancelRequest]) -> bool:
            open_orders = await source.open_orders()
            if not venue_proves_closed(requests, open_orders, await source.historical_orders()):
                return False
            if live is not None:
                live.mark_closed(requests)
            report(f"LIVE: order history proves {len(requests)} late-receipt cancel(s) closed")
            return True

        async def engine_halted(error: BaseException | None) -> bool:
            # Glyde's health is the authority; a refusal code decides only when it is unreachable.
            probe = getattr(source, "engine_ready", None)
            ready = await probe() if probe is not None else None
            return engine_unavailable(error) if ready is None else not ready

        managed = RiskManagedClient(
            raw,
            guard,
            stop_event=stop,
            cleanup_symbols=config.symbols,
            metrics=metrics,
            # A stale book pauses only its own market; the runner bounds how long.
            isolate_stale_markets=True,
            order_proof=prove_closed if paper is None else None,
            proof_window_seconds=float(config.risk.max_stale_pause_seconds),
            engine_halted=engine_halted,
            halt_window_seconds=float(config.risk.max_engine_halt_seconds),
        )
        strategies: dict[str, Strategy] = {}
        stream: AsyncGenerator[JsonObject, None] | None = None
        reference_stream: AsyncGenerator[JsonObject, None] | None = None
        resources = AsyncExitStack()
        tasks: list[asyncio.Task[None] | asyncio.Task[bool]] = []
        risk_stop = None

        async def observe_paper() -> None:
            if paper_result is not None and paper is not None:
                await paper_result.observe(paper, metrics)

        try:
            if stop.is_set():
                return
            metadata = await _wait_out_halt(
                source, stop, report, float(config.risk.max_engine_halt_seconds), source.meta
            )
            if metadata is None or stop.is_set():
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

            async def open_streams() -> dict[str, Book]:
                nonlocal stream, reference_stream
                stream = live.stream() if live is not None else source.stream(subscriptions)
                resources.push_async_callback(stream.aclose)
                books: dict[str, Book] = {}
                async with asyncio.timeout(10):
                    while books.keys() != selected.keys():
                        try:
                            frame = await anext(stream)
                        except StopAsyncIteration:
                            raise StreamDisconnected("book stream disconnected") from None
                        if frame.get("channel") == "l2Book":
                            book = book_from_wire(frame["data"])
                            if book.symbol not in selected:
                                raise OneApiError("book market mismatch")
                            books[book.symbol] = book
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
                                if frame.get("channel") == "l2Book":
                                    book = book_from_wire(frame["data"])
                                    if book.symbol not in references:
                                        raise RiskViolation("reference_market_mismatch")
                                    references[book.symbol].update(book)
                                    received.add(book.symbol)
                        except StopAsyncIteration:
                            raise RiskViolation("reference_disconnected") from None
                return books

            async def start_quoting(books: dict[str, Book], first: bool) -> None:
                nonlocal cleanup_allowed
                for book in books.values():
                    guard.update_book(book)
                    metrics.book(guard.books[book.symbol])
                    if paper is not None:
                        await paper.on_book(book)
                        await observe_paper()
                if live is not None and first:
                    await check_live_exposure(
                        live, config.symbols, allow_existing_exposure=allow_existing_exposure
                    )
                    cleanup_allowed = True
                    # A previous run killed mid-quote (watchdog, OOM) leaves tagged orders behind.
                    for symbol in config.symbols:
                        await managed.cancel_all(symbol)
                elif live is not None:
                    # Reconcile: cancel what the fresh venue snapshot still shows before quoting.
                    for symbol in config.symbols:
                        await managed.cancel_all(symbol)
                await managed.observe_account()
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
            paused_since: dict[str, float] = {}
            market_index = {symbol: index for index, symbol in enumerate(selected)}
            max_pause = float(config.risk.max_stale_pause_seconds)

            async def pause_market(symbol: str, error: StaleMarket) -> None:
                if symbol not in paused:
                    await strategies[symbol].stop()
                    paused_since[symbol] = time.monotonic()
                    reason = str(error) if error.symbol == symbol else "stale_peer"
                    journal.emit("book_stale", market=market_index[symbol], reason=reason)
                    metrics.count("stale_pauses")
                    if paper_result is not None:
                        paper_result.pause("stale")
                    report(f"{config.mode.upper()} {symbol}: quoting paused (stale {error.symbol})")
                elif time.monotonic() - paused_since[symbol] > max_pause:
                    raise error  # Stale past the pause bound: end the run as a risk stop.
                paused[symbol] = error.symbol

            async def refresh_markets() -> None:
                for symbol in selected:
                    try:
                        book = guard.check_fresh(symbol)
                        dependency = paused.get(symbol, symbol)
                        # Confirmed retirement of a flat peer removes its valuation dependency.
                        if dependency != symbol and guard.has_market_exposure(dependency):
                            guard.check_fresh(dependency)
                    except StaleMarket as error:
                        await pause_market(symbol, error)
                        continue
                    if symbol in paused:
                        # Reconcile as a reconnect does: retire what the venue still shows.
                        await managed.cancel_all(symbol)
                        await managed.observe_account()
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
                        paused_ms = round((time.monotonic() - paused_since.pop(symbol)) * 1000)
                        journal.emit("book_fresh", market=market_index[symbol], paused_ms=paused_ms)
                        if paper_result is not None and not paused:
                            paper_result.resume("stale")
                        report(f"{config.mode.upper()} {symbol}: quoting resumed")

            fill_index = 0
            book_messages = 0

            async def deliver_fills() -> None:
                nonlocal fill_index
                assert paper is not None
                for fill in paper.fills[fill_index:]:
                    await strategies[fill.symbol].on_fill(fill)
                fill_index = len(paper.fills)
                metrics.paper_fills(paper)

            async def receive_books() -> None:
                nonlocal book_messages
                assert stream is not None
                async for frame in stream:
                    if paper is not None and frame.get("channel") in {
                        "resnapshot_required",
                        "glydeSafety",
                    }:
                        if paper_result is not None:
                            paper_result.pause("gap")
                        raise OneApiError("paper source requires a fresh snapshot")
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
                            await observe_paper()
                        await deliver_fills()
                        await managed.observe_account()
                        continue
                    if frame.get("channel") != "l2Book":
                        continue
                    book = book_from_wire(frame["data"])
                    if book.symbol not in selected:
                        raise OneApiError("book market mismatch")
                    guard.update_book(book)
                    metrics.book(guard.books[book.symbol])
                    if book.symbol in references:
                        # Reference deviation check; staleness itself pauses via the watchdog.
                        with suppress(StaleMarket):
                            guard.check_fresh(book.symbol)
                    book_messages += 1
                    if paper is not None:
                        await paper.on_book(book)
                        await observe_paper()
                        await deliver_fills()
                    strategy = strategies[book.symbol]
                    strategy.context = replace(
                        strategy.context, price_tick=_price_tick(book, precision[book.symbol])
                    )
                    await strategy.on_book(book)
                raise StreamDisconnected("book stream disconnected")

            async def receive_reference() -> None:
                assert reference_stream is not None
                async for frame in reference_stream:
                    if frame.get("channel") == "l2Book":
                        book = book_from_wire(frame["data"])
                        if book.symbol not in references:
                            raise RiskViolation("reference_market_mismatch")
                        references[book.symbol].update(book)
                        with suppress(StaleMarket):
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
                        await managed.observe_account()
                        refreshed = now
                    async with market_lock:
                        await refresh_markets()
                        for symbol, strategy in strategies.items():
                            if stop.is_set():
                                return
                            if symbol in paused:
                                continue
                            try:
                                await strategy.on_tick()
                            except StaleMarket as error:
                                if error.symbol not in selected:
                                    raise
                                await pause_market(error.symbol, error)
                                await pause_market(symbol, error)
                            if paper is not None:
                                metrics.paper_fills(paper)
                                await observe_paper()
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
                        await refresh_markets()
                        await managed.check_liveness()
                    await asyncio.sleep(0.25)

            async def serve() -> StreamDisconnected | None:
                nonlocal tasks
                tasks = [
                    asyncio.create_task(receive_books()),
                    asyncio.create_task(drive()),
                    asyncio.create_task(watchdog()),
                    asyncio.create_task(stop.wait()),
                ]
                if references:
                    tasks.insert(0, asyncio.create_task(receive_reference()))
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                dropped = None
                for task in done:
                    if task is tasks[-1]:
                        continue
                    try:
                        task.result()
                    except RiskViolation as error:
                        if not stop.is_set() or str(error) != "client_closed":
                            raise
                    except StreamDisconnected as error:
                        dropped = dropped or error
                    except TimeoutError:
                        # A late read (e.g. the owner cut after a cancel) pauses and reconnects.
                        dropped = dropped or StreamDisconnected("live read deadline passed")
                    except OneApiError as error:
                        # A transient One API read failure (5xx, 429) mid-run is a drop too.
                        if not reconnectable(error):
                            raise
                        dropped = dropped or StreamDisconnected("transient One API failure")
                    else:
                        if not stop.is_set():
                            raise OneApiError("strategy task exited unexpectedly")
                return None if stop.is_set() else dropped

            keep_quotes = (
                float(config.risk.keep_quotes_on_disconnect_seconds) if live is not None else 0.0
            )
            sweep_due: float | None = None  # Quotes kept through a drop are swept at this time.

            async def retire(dropped: StreamDisconnected) -> None:
                nonlocal tasks, sweep_due
                if paper_result is not None:
                    paper_result.pause("disconnect")
                results, tasks = await managed.interrupt(tasks), []
                if live is not None:
                    # Retire on receipts alone; the next connection's snapshot reconciles.
                    live.invalidate("live stream retired", disconnected=True)
                for result in results:
                    if isinstance(result, Exception) and not isinstance(
                        result, (StreamDisconnected, TimeoutError)
                    ):
                        if stop.is_set():
                            return
                        raise result
                # An unknown outcome or risk halt still ends the run; only flat state reconnects.
                if not guard.resume_after_disconnect():
                    raise dropped
                if keep_quotes:
                    # Resting quotes stay up through a short outage; reconnect reconciles them.
                    sweep_due = time.monotonic() + keep_quotes
                else:
                    for strategy in strategies.values():
                        await strategy.stop()
                strategies.clear()
                paused.clear()
                paused_since.clear()
                if paper_result is not None:
                    paper_result.resume("stale")  # Reconnect restarts every market fresh.
                await resources.aclose()
                if sweep_due is None:
                    await sweep()
                if live is not None:
                    await live.close()
                await observer.publish(guard, paper=paper)

            async def sweep() -> None:
                for symbol in config.symbols:
                    # The next connection's reconcile repeats a deferred sweep before quoting.
                    with suppress(VenueSweepDeferred):
                        await managed.cancel_all(symbol)

            max_halt = float(config.risk.max_engine_halt_seconds)
            first, attempts, dropped_at = True, 0, time.monotonic()
            failing_since = dropped_at

            async def give_up_after_window(attempt: str, error: BaseException) -> bool:
                nonlocal failing_since
                now = time.monotonic()
                if await engine_halted(error):
                    # Halt time never counts against the reconnect window; it has its own budget.
                    failing_since = now
                    if now - dropped_at <= max_halt:
                        return False
                    report(
                        f"{config.mode.upper()}: {attempt} failed; Glyde engine halted past the "
                        f"{max_halt:g}s halt budget; stopping"
                    )
                    return True
                if now - failing_since <= max_pause:
                    return False
                report(
                    f"{config.mode.upper()}: {attempt} failed past the {max_pause:g}s "
                    "reconnect window; stopping"
                )
                return True

            retry_after: float | None = None  # Set once a connection must be retried.

            async def back_off(seconds: float) -> bool:
                """Wait before reconnecting, sweeping kept quotes when their bound passes."""
                nonlocal sweep_due
                resume_at = time.monotonic() + seconds
                while True:
                    now = time.monotonic()
                    if sweep_due is not None and now >= sweep_due:
                        report(
                            f"{config.mode.upper()}: still disconnected after {keep_quotes:g}s; "
                            "cancelling kept quotes"
                        )
                        await sweep()
                        sweep_due = None
                    if now >= resume_at:
                        return False
                    until = resume_at if sweep_due is None else min(resume_at, sweep_due)
                    if await _stopped_within(stop, max(0.0, until - now)):
                        return True

            while True:
                if retry_after is not None:
                    attempts += 1
                    delay = RECONNECT_INITIAL_SECONDS * 2 ** min(attempts - 1, 16)
                    delay = min(delay, RECONNECT_MAX_SECONDS) * random.uniform(0.5, 1)
                    if await back_off(max(delay, retry_after)):
                        return
                    if live is not None:
                        live = LiveStateClient(source, config, metrics=metrics)
                        raw = managed.client = live
                try:
                    books = await _unless_stopped(stop, open_streams())
                except Exception as error:
                    # A deferred subscription, or a start while Glyde reports its engine
                    # halted, is retried even before the first quote.
                    if not reconnectable(error) or (
                        first
                        and not isinstance(error, SubscriptionDeferred)
                        and not await engine_halted(error)
                    ):
                        raise
                    attempt = f"connect {attempts + 1}" if first else f"reconnect {attempts}"
                    report(
                        f"{config.mode.upper()}: {attempt} attempt failed ({_failure_label(error)})"
                    )
                    if await give_up_after_window(attempt, error):
                        raise
                    await resources.aclose()
                    if live is not None:
                        await live.close()
                    retry_after = _retry_floor(error)
                    continue
                if books is None or stop.is_set():
                    return
                try:
                    await start_quoting(books, first)
                except Exception as error:
                    # A deferral can arrive after the books, while waiting for the account cut;
                    # a slow reconnect reconcile can also outlast the first books' freshness.
                    retryable = reconnectable(error) or (
                        not first and isinstance(error, StaleMarket) and guard.halted is None
                    )
                    if not retryable or (
                        first
                        and not isinstance(error, SubscriptionDeferred)
                        and not await engine_halted(error)
                    ):
                        raise
                    attempt = f"connect {attempts + 1}" if first else f"reconnect {attempts}"
                    report(
                        f"{config.mode.upper()}: {attempt} attempt failed ({_failure_label(error)})"
                    )
                    if await give_up_after_window(attempt, error):
                        raise
                    for strategy in strategies.values():
                        await strategy.stop()
                    strategies.clear()
                    await resources.aclose()
                    if cleanup_allowed and not isinstance(error, VenueSweepDeferred):
                        await sweep()
                    if live is not None:
                        await live.close()
                    retry_after = _retry_floor(error)
                    continue
                sweep_due = None  # The reconnect reconcile cancelled any kept quotes.
                if stop.is_set():
                    return
                if not first:
                    downtime_ms = round((time.monotonic() - dropped_at) * 1000)
                    journal.emit("reconnected", attempts=attempts, downtime_ms=downtime_ms)
                    report(f"{config.mode.upper()}: reconnected after {attempts} attempt(s)")
                    if paper_result is not None:
                        paper_result.resume("disconnect")
                first, attempts = False, 0
                dropped = await serve()
                if dropped is None:
                    return
                dropped_at = time.monotonic()
                journal.emit("disconnected")
                if isinstance(dropped, StreamFault):
                    journal.emit("stream_fault", **dropped.fields())
                await retire(dropped)
                failing_since = time.monotonic()  # A sweep waiting out a halt is not counted.
                retry_after = _retry_floor(dropped)
                report(
                    f"{config.mode.upper()}: stream disconnected ({_failure_label(dropped)}); "
                    + (
                        f"quotes kept up to {keep_quotes:g}s, reconnecting"
                        if sweep_due is not None
                        else "quotes retired, reconnecting"
                    )
                )
        except BaseException as error:
            if isinstance(error, RiskViolation):
                risk_stop = str(error)
                if paper_result is not None and str(error) in {
                    "stale_book",
                    "stale_book_source",
                    "stale_inputs",
                    "stale_account",
                }:
                    paper_result.pause("stale")
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
                            if paper_result is not None and paper is not None:
                                paper_result.cleanup_confirmed = not await paper.open_orders()
                    finally:
                        if live is not None:
                            await live.close()
                        if paper is not None:
                            await observe_paper()
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
