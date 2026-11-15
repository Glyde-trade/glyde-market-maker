"""Opt-in Avellaneda source-time inputs and the real offline runner/risk path."""

import asyncio
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal as D

import httpx
import pytest

from glyde_mm.client import OneApiClient
from glyde_mm.config import Config, RiskConfig
from glyde_mm.models import Account, Book, Level, Trade
from glyde_mm.paper import PaperExchange
from glyde_mm.replay import replay
from glyde_mm.risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation
from glyde_mm.runner import run
from glyde_mm.strategies import AvellanedaConfig, AvellanedaMarketMakingStrategy, StrategyContext
from glyde_mm.strategies.factory import create_strategy
from glyde_mm.strategies.stream_volatility import BookTradeVolatility
from glyde_mm.testing import FakeOneApiServer


def book(mid, timestamp):
    mid = D(mid)
    return Book("BTC", (Level(mid - 1, D(2)),), (Level(mid + 1, D(2)),), timestamp)


def trade(price, timestamp, identity="1"):
    return Trade("BTC", "buy", D(1), D(price), timestamp, identity)


def complete(estimator, timestamp, mid, price):
    estimator.on_book(book(mid, timestamp))
    estimator.on_trade(trade(price, timestamp, str(timestamp)))


def test_closed_intervals_combine_book_and_trade_vwap_without_callback_samples():
    estimate = BookTradeVolatility(2, 1000)
    complete(estimate, 0, 100, 102)
    estimate.on_trade(trade(98, 1, "second"))
    estimate.on_trade(trade(98, 1, "second"))
    for stamp in range(2, 1000):
        estimate.on_book(book(100, stamp))
    assert estimate.value is None and not estimate.prices
    complete(estimate, 1000, 102, 102)
    complete(estimate, 2000, 100, 100)
    assert estimate.value is None
    estimate.on_book(book(100, 3000))
    assert tuple(estimate.prices) == (D(100), D(102), D(100))
    assert estimate.value == 2
    estimate.on_trade(trade(999, 1000, "late"))
    assert estimate.value == 2


@pytest.mark.parametrize("missing", ["book", "trades", "gap"])
def test_missing_stream_or_source_interval_invalidates_window(missing):
    estimate = BookTradeVolatility(2, 1000)
    for stamp, price in [(0, 100), (1000, 101), (2000, 100)]:
        complete(estimate, stamp, price, price)
    estimate.on_book(book(100, 3000))
    assert estimate.value == 1
    if missing == "book":
        estimate.on_trade(trade(101, 4000))
        estimate.on_trade(trade(100, 5000, "2"))
    elif missing == "trades":
        estimate.on_book(book(101, 4000))
    else:
        estimate.on_book(book(101, 6000))
    assert estimate.value is None


def test_conflicting_trade_identity_and_bounded_interval_refuse():
    estimate = BookTradeVolatility(2, 1000)
    estimate.on_trade(trade(100, 0))
    with pytest.raises(RiskViolation, match="volatility_trade_conflict"):
        estimate.on_trade(trade(101, 0))
    for index in range(1, 4096):
        estimate.on_trade(trade(100, 0, str(index + 1)))
    with pytest.raises(RiskViolation, match="volatility_trade_budget"):
        estimate.on_trade(trade(100, 0, "overflow"))


async def test_ratio_inventory_and_depth_parameters_keep_normalized_model():
    paper = PaperExchange(D(1000))
    strategy = AvellanedaMarketMakingStrategy(
        StrategyContext(paper, "BTC", D("0.01"), D("0.001")),
        AvellanedaConfig(volatility_samples=2, volatility_source="book_and_trades"),
    )
    for timestamp, price in [(0, 100), (1000, 102), (2000, 100)]:
        await strategy.on_book(book(price, timestamp))
        await strategy.on_trade(trade(price, timestamp, str(timestamp)))
    await strategy.on_book(book(100, 3000))
    neutral = Account(D(1000))
    middle, spread = strategy.model_prices(strategy.book, neutral)
    assert middle == 100
    assert (
        strategy.model_prices(strategy.book, replace(neutral, positions={"BTC": D(1)}))[0] < middle
    )
    strategy.model = replace(strategy.model, inventory_target_position=D(1))
    assert (
        strategy.model_prices(strategy.book, replace(neutral, positions={"BTC": D(1)}))[0] == middle
    )
    strategy.model = replace(strategy.model, liquidity_kappa=D(2))
    assert strategy.model_prices(strategy.book, neutral)[1] < spread
    strategy.model = replace(strategy.model, risk_factor=D(2))
    assert strategy.model_prices(strategy.book, neutral)[1] > spread
    await strategy.on_book(book(100, 4000))
    assert strategy.volatility is None and strategy._needs_refresh


async def test_missing_trade_interval_retires_real_quotes_before_refresh(tmp_path):
    clock = [0.0]

    class TimedPaper(PaperExchange):
        async def account(self):
            return replace(await super().account(), received_at=clock[0])

    paper = TimedPaper(D(1000), clock_ms=lambda: round(clock[0] * 1000))
    guard = RiskGuard(
        RiskLimits(max_position=D(1), max_order_notional=D(25)),
        clock=lambda: clock[0],
        wall_clock=lambda: clock[0],
        state_path=tmp_path / "risk.json",
    )
    managed = RiskManagedClient(paper, guard)
    strategy = AvellanedaMarketMakingStrategy(
        StrategyContext(managed, "BTC", D("0.01"), D("0.001"), clock=lambda: clock[0]),
        AvellanedaConfig(
            volatility_samples=2,
            volatility_source="book_and_trades",
            order_amount=D("0.1"),
        ),
    )

    async def feed(seconds, mid):
        clock[0] = seconds
        current = replace(book(mid, seconds * 1000), received_at=seconds)
        await paper.on_book(current)
        guard.update_book(current)
        await strategy.on_book(current)

    for seconds, price in [(0, 100), (1, 101), (2, 100)]:
        await feed(seconds, price)
        await strategy.on_trade(trade(price, seconds * 1000, str(seconds)))
    await feed(3, 100)
    await strategy.on_tick()
    assert len(await paper.open_orders()) == 2 and len(guard.pending) == 2
    await feed(4, 100)
    await strategy.on_tick()
    assert strategy.volatility is None
    assert await paper.open_orders() == [] and guard.pending == {}
    await managed.close()


@pytest.mark.parametrize("source", ["guessed", None, []])
def test_unknown_volatility_source_refuses(source):
    with pytest.raises(ValueError, match="volatility_source"):
        AvellanedaConfig(volatility_source=source)


@pytest.mark.parametrize("mode", ["live", "replay"])
async def test_non_streaming_modes_refuse_before_transport_or_recording(tmp_path, mode):
    config = Config(
        mode="live",
        strategy="avellaneda_market_making",
        strategy_config={"volatility_source": "book_and_trades"},
    )
    with pytest.raises(ValueError, match="book_and_trades"):
        if mode == "live":
            await run(
                config, state_directory=tmp_path, client_factory=lambda _: pytest.fail("transport")
            )
        else:
            await replay(config, tmp_path / "absent.jsonl", 1)


@pytest.mark.parametrize("limited", [False, True])
async def test_actual_fake_one_api_runner_samples_both_channels_and_keeps_risk(
    monkeypatch, tmp_path, limited
):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline paper only")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(Config, "secret", forbidden)
    fake = FakeOneApiServer(timestamp_ms=time.time_ns() // 1_000_000)
    fake.set_book("BTC", bids=[("99", "2")], asks=[("101", "2")])
    started, stop = asyncio.Event(), asyncio.Event()
    observed = []
    submissions = []

    def strategy_factory(name, context, values):
        strategy = create_strategy(name, context, values)
        observed.append(strategy)
        started.set()
        return strategy

    class Paper(PaperExchange):
        async def place_orders(self, orders):
            submissions.extend(orders)
            result = await super().place_orders(orders)
            stop.set()
            return result

    paper = Paper(D(1000))

    async def emit():
        await started.wait()
        for index, mid in enumerate([100, 101, 100, 102]):
            if index:
                fake.advance_time(10)
            fake.set_book("BTC", bids=[(str(mid - 1), "2")], asks=[(str(mid + 1), "2")])
            fake.market_order("BTC", buy=False, size="0.1")
            await asyncio.sleep(0)

    @asynccontextmanager
    async def client_factory(config):
        async with httpx.AsyncClient(transport=fake.http_transport()) as http:
            async with OneApiClient(config, http_client=http, connect_ws=fake.connect_ws) as client:
                emitter = asyncio.create_task(emit())
                try:
                    yield client
                finally:
                    emitter.cancel()
                    await asyncio.gather(emitter, return_exceptions=True)

    config = Config(
        mode="paper",
        base_url="https://offline.invalid/v1",
        paper_balance=D(1000),
        strategy="avellaneda_market_making",
        strategy_config={
            "volatility_source": "book_and_trades",
            "volatility_samples": 2,
            "sample_interval_ms": 10,
            "order_amount": "0.1",
        },
        risk=RiskConfig(max_position=D("0.01" if limited else "1"), max_order_notional=D(25)),
    )
    async with asyncio.timeout(3):
        task = run(
            config,
            state_directory=tmp_path,
            client_factory=client_factory,
            paper_factory=lambda _: paper,
            strategy_factory=strategy_factory,
            stop_event=stop,
            report=lambda _: None,
        )
        if limited:
            with pytest.raises(RiskViolation, match="max_position"):
                await task
        else:
            await task
    assert observed[0].volatility == 1
    assert [row.side for row in submissions] == ([] if limited else ["buy", "sell"])
    assert await paper.open_orders() == []
    assert not fake._connections
    assert all(row["body"]["type"] == "meta" for row in fake.requests)
