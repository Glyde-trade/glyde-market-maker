"""Ten simulated minutes per strategy over real codec/cache/risk and an offline ledger.

Faults retire the session; recovery is an explicit new session with a full snapshot,
not automatic reconnect or a claim that persisted uncertainty may be cleared.
"""

import asyncio
import random
import socket
import time
from collections import Counter
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import httpx
import pytest
from live_fake import SequencedFake
from live_wire import batch, owner_roots

from glyde_mm import client as client_module
from glyde_mm import live_state as state_module
from glyde_mm.client import OneApiClient, OneApiError
from glyde_mm.config import Config, RiskConfig
from glyde_mm.live_state import LiveStateClient
from glyde_mm.models import OrderRequest
from glyde_mm.risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation
from glyde_mm.strategies import StrategyContext, create_strategy

OWNER = "0x" + "1" * 40


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.origin = float(int(time.time()))

    def monotonic(self):
        return self.seconds

    def time(self):
        return self.origin + self.seconds


class SoakWire(SequencedFake):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.reject = False
        self.attempts = []
        self.accepted_ids = set()
        self.refusals = 0
        self.guard = None

    def http(self, request):
        import json

        envelope = json.loads(request.content)
        action = envelope.get("action", {})
        if action.get("type") == "order":
            orders = action["orders"]
            ids = [row["c"] for row in orders]
            assert len(set(ids)) == len(ids)
            assert not set(ids) & self.accepted_ids, "duplicate order dispatched"
            self.accepted_ids.update(ids)
            self.attempts.extend([self.clock.seconds] * len(orders))
            # Check accepted + submitted exposure independently of risk reservations.
            self.assert_limits(orders)
            if self.reject:
                self.requests.append(envelope)
                self.refusals += len(orders)
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "response": {
                            "type": "order",
                            "data": {"statuses": [{"error": "injected refusal"} for _ in orders]},
                        },
                    },
                )
        response = super().http(request)
        self.assert_limits()
        return response

    def assert_limits(self, proposed=()):
        rows = self.ledger.info({"type": "openOrders", "user": OWNER})
        assert len({str(row["oid"]) for row in rows}) == len(rows)
        assert len({row["cloid"] for row in rows}) == len(rows)
        position = self.ledger.info({"type": "clearinghouseState", "user": OWNER})
        size = sum((D(row["position"]["szi"]) for row in position["assetPositions"]), D(0))
        buys = sum((D(row["sz"]) for row in rows if row["side"] == "B"), D(0))
        sells = sum((D(row["sz"]) for row in rows if row["side"] == "A"), D(0))
        for row in proposed:
            quantity, price = D(row["s"]), D(row["p"])
            assert quantity * max(price, D(102)) <= D(5)
            if row["b"]:
                buys += quantity
            else:
                sells += quantity
        assert size + buys <= D("0.25")
        assert size - sells >= D("-0.25")
        assert max(abs(size + buys), abs(size - sells)) * D(104) <= D(30)
        assert D(position["marginSummary"]["accountValue"]) > D(9995)
        assert sum(t > self.clock.seconds - 60 for t in self.attempts) <= 600
        if proposed and self.guard is not None:
            assert self.guard.halted is None


class Session:
    def __init__(self, wire, source, config, clock):
        self.wire, self.clock = wire, clock
        self.cache = LiveStateClient(source, config)
        self.guard = RiskGuard(
            RiskLimits.from_config(config.risk), clock=clock.monotonic, wall_clock=clock.time
        )
        wire.guard = self.guard
        self.managed = RiskManagedClient(self.cache, self.guard, cleanup_symbol="BTC")
        self.strategy = create_strategy(
            config.strategy,
            StrategyContext(self.managed, "BTC", D("0.01"), D("0.00001"), clock=clock.monotonic),
            config.strategy_config,
        )
        self.stream = self.cache.stream()
        self.failure = None
        self.pump = asyncio.create_task(self.receive())

    async def receive(self):
        try:
            async for frame in self.stream:
                if frame["channel"] == "l2Book":
                    book = client_module.book_from_wire(frame["data"])
                    self.guard.update_book(book)
                    await self.strategy.on_book(book)
                else:
                    self.guard.update_account(await self.cache.account())
                    self.guard.set_open_orders(await self.cache.open_orders())
                    for fill in frame["fills"]:
                        await self.strategy.on_fill(fill)
        except OneApiError as error:
            self.failure = error

    async def settle(self):
        # Drain finite in-process WS work without advancing source or asyncio time.
        for _ in range(16):
            await asyncio.sleep(0)
        if self.failure is None:
            assert self.wire.socket.queue.empty()
            assert self.cache._events.empty()

    async def tick(self):
        await self.settle()
        await self.strategy.on_tick()
        await self.settle()
        rows = self.wire.ledger.info({"type": "openOrders", "user": OWNER})
        assert {str(row["oid"]) for row in rows} <= set(self.strategy._active), "orphan order"
        self.wire.assert_limits()

    async def close(self, *, fault=False):
        # The real cache latches faults. Cleanup still cancels original IDs, while
        # failed read confirmation must remain an error, never a recovery signal.
        self.wire.guard = None
        try:
            await self.strategy.stop()
            await self.managed.close()
        except (OneApiError, RiskViolation):
            if not fault:
                raise
        finally:
            self.pump.cancel()
            await asyncio.gather(self.pump, return_exceptions=True)
            await self.stream.aclose()
            await self.cache.close()
        assert self.wire.ledger.info({"type": "openOrders", "user": OWNER}) == []


def selected(kind):
    options = {"order_amount": "0.01", "order_refresh_time": 2.0, "order_levels": 3}
    if kind == "avellaneda":
        options.update(volatility_samples=2, liquidity_kappa="5")
    else:
        options.update(bid_spread="0.003", ask_spread="0.003")
    return Config(
        mode="live",
        owner=OWNER,
        vault_address="0x" + "4" * 40,
        strategy="avellaneda_market_making" if kind == "avellaneda" else "pure_market_making",
        strategy_config=options,
        risk=RiskConfig(
            max_position=D("0.25"),
            max_order_notional=D(5),
            max_total_notional=D(30),
            max_daily_loss=D(5),
            max_orders_per_minute=600,
            stale_book_seconds=D(3),
        ),
    )


@pytest.mark.offline_soak
@pytest.mark.parametrize("kind", ["levels", "avellaneda"])
async def test_ten_minute_seeded_ws_soak(kind, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline soak attempted a real socket")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "0x" + "3" * 64)
    monkeypatch.setenv("GLYDE_READ_API_KEY", "read-fixture")
    clock = Clock()
    monkeypatch.setattr(
        state_module, "time", SimpleNamespace(time=clock.time, monotonic=clock.monotonic)
    )
    real_book, real_account = client_module.book_from_wire, state_module.account_from_wire
    monkeypatch.setattr(
        client_module,
        "book_from_wire",
        lambda data: replace(real_book(data), received_at=clock.seconds),
    )
    monkeypatch.setattr(state_module, "book_from_wire", client_module.book_from_wire)
    monkeypatch.setattr(
        state_module,
        "account_from_wire",
        lambda data: replace(real_account(data), received_at=clock.seconds),
    )
    rng = random.Random(6106264)
    wire = SoakWire(clock)
    config = selected(kind)
    counts = Counter()
    session = None
    http = httpx.AsyncClient(transport=httpx.MockTransport(wire.http))
    source = OneApiClient(config, http_client=http, connect_ws=wire.connect)
    await source.meta()
    # Guaranteed coverage, random fault times/order and matching side/price path.
    faults = ["gap", "disconnect", "stale"] * 3
    rng.shuffle(faults)
    fault_times = {40 + 60 * i + rng.randrange(10): fault for i, fault in enumerate(faults)}
    stale_until = -1
    try:
        for second in range(600):
            clock.seconds = float(second)
            wire.ledger.now_ms = round(clock.time() * 1000)
            wire.guard = None
            mid = D(100) + D(rng.randrange(-10, 11)) / 100
            wire.ledger.set_book("BTC", bids=[(str(mid - 1), "10")], asks=[(str(mid + 1), "10")])
            if session is None:
                # Delta-only reconnect cannot install owner authority or dispatch.
                bad = LiveStateClient(source, config)
                with pytest.raises(OneApiError):
                    for frame in batch(
                        "mm-owner", owner_roots(OWNER), owner=OWNER, sequence=2, previous=1
                    ):
                        bad.consume(frame)
                with pytest.raises(OneApiError):
                    await bad.place_orders([OrderRequest("BTC", "buy", D("0.01"), D(99))])
                await bad.close()
                wire.sequence = wire.book_sequence = 0
                session = Session(wire, source, config, clock)
                await session.settle()
                counts["fresh_snapshot"] += 1
            wire.guard = session.guard
            if second in fault_times:
                fault = fault_times[second]
                counts[fault] += 1
                before = len(wire.attempts)
                if fault == "gap":
                    wire.emit_owner(skip=1)
                elif fault == "disconnect":
                    await wire.socket.close()
                else:
                    stale_until = second + 4
                if fault != "stale":
                    await session.settle()
                    assert session.failure is not None
                    with pytest.raises(OneApiError):
                        session.cache.check()
                    with pytest.raises((OneApiError, RiskViolation)):
                        await session.managed.place_orders(
                            [OrderRequest("BTC", "buy", D("0.01"), D(99))]
                        )
                    assert len(wire.attempts) == before
                    await session.close(fault=True)
                    session = None
                    continue
            # Refusals are definite order outcomes, never an implicit retry.
            wire.reject = second % 17 == 0
            if not second <= stale_until:
                wire.emit_book()
            wire.emit_owner()
            await session.settle()
            if clock.seconds - session.guard.books["BTC"].received_at > 3:
                before = len(wire.attempts)
                with pytest.raises(RiskViolation, match="stale"):
                    session.guard.check_fresh("BTC")
                with pytest.raises(RiskViolation):
                    await session.managed.place_orders(
                        [OrderRequest("BTC", "buy", D("0.01"), D(99))]
                    )
                assert len(wire.attempts) == before
                await session.close()
                session = None
                stale_until = -1
                continue
            await session.tick()
            rows = wire.ledger.info({"type": "openOrders", "user": OWNER})
            if rows and second % 7 == 0:
                row = rng.choice(rows)
                before = {str(r["oid"]): D(r["sz"]) for r in rows}
                wire.ledger.market_order("BTC", buy=row["side"] == "A", size="0.005")
                wire.emit_owner()
                await session.settle()
                after = wire.ledger.info({"type": "openOrders", "user": OWNER})
                if any(
                    str(r["oid"]) in before and D(r["sz"]) < before[str(r["oid"])] for r in after
                ):
                    counts["partial_fill"] += 1
                wire.assert_limits()
            counts["tick"] += 1
        assert clock.seconds == 599
        assert all(counts[name] >= 3 for name in ("gap", "disconnect", "stale"))
        assert counts["partial_fill"] > 10 and counts["tick"] > 500
        assert counts["fresh_snapshot"] == 10
        assert wire.refusals > 0 and len(wire.attempts) > 100
        print(f"SOAK {kind}: 600 simulated seconds, {dict(counts)}, refused={wire.refusals}")
    finally:
        wire.guard = None
        if session is not None:
            await session.close(fault=session.failure is not None)
        await source.close()
    assert [r["type"] for r in wire.requests if "type" in r] == ["meta"]


@pytest.mark.offline_soak
async def test_ten_minute_book_and_trade_paper_soak(monkeypatch):
    from glyde_mm.client import trades_from_wire
    from glyde_mm.paper import PaperExchange
    from glyde_mm.testing import FakeOneApiServer

    def forbidden(*args, **kwargs):
        raise AssertionError("paper soak attempted network or credential access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(Config, "secret", forbidden)
    clock = Clock()
    rng = random.Random(6264)
    fake = FakeOneApiServer(timestamp_ms=round(clock.time() * 1000))
    fake.set_book("BTC", bids=[("99", "10")], asks=[("101", "10")])

    class TimedPaper(PaperExchange):
        async def account(self):
            return replace(await super().account(), received_at=clock.seconds)

    paper = TimedPaper(D(10000), clock_ms=lambda: round(clock.time() * 1000))
    paper.use_trade_tape()
    config = replace(
        selected("avellaneda"),
        mode="paper",
        strategy_config={
            **selected("avellaneda").strategy_config,
            "volatility_source": "book_and_trades",
            "order_refresh_time": 30,
        },
    )
    guard = RiskGuard(
        RiskLimits.from_config(config.risk), clock=clock.monotonic, wall_clock=clock.time
    )
    managed = RiskManagedClient(paper, guard, cleanup_symbol="BTC")
    strategy = create_strategy(
        config.strategy,
        StrategyContext(managed, "BTC", D("0.01"), D("0.00001"), clock=clock.monotonic),
        config.strategy_config,
    )
    fill_index = 0
    counts = Counter()
    async with httpx.AsyncClient(transport=fake.http_transport()) as http:
        async with OneApiClient(config, http_client=http, connect_ws=fake.connect_ws) as source:
            await source.meta()
            stream = source.stream(
                [{"type": "l2Book", "coin": "BTC"}, {"type": "trades", "coin": "BTC"}]
            )

            async def receive():
                nonlocal fill_index
                async for frame in stream:
                    if frame["channel"] == "l2Book":
                        book = replace(
                            client_module.book_from_wire(frame["data"]), received_at=clock.seconds
                        )
                        guard.update_book(book)
                        await paper.on_book(book)
                        await strategy.on_book(book)
                    elif frame["channel"] == "trades":
                        for trade in trades_from_wire(frame["data"]):
                            await strategy.on_trade(trade)
                            await paper.on_trade(trade)
                    for fill in paper.fills[fill_index:]:
                        await strategy.on_fill(fill)
                    fill_index = len(paper.fills)
                    guard.update_account(await paper.account())

            task = asyncio.create_task(receive())
            try:
                for second in range(600):
                    clock.seconds = float(second)
                    fake.now_ms = round(clock.time() * 1000)
                    mid = D(100) + D(rng.randrange(-10, 11)) / 100
                    fake.set_book("BTC", bids=[(str(mid - 1), "10")], asks=[(str(mid + 1), "10")])
                    # Every twenty seconds lose one public trade interval. Model
                    # readiness must retire quotes before the 30-second refresh.
                    if second % 20 != 10:
                        fake.market_order("BTC", buy=bool(rng.randrange(2)), size="0.005")
                    for _ in range(16):
                        await asyncio.sleep(0)
                    assert not task.done(), "public stream receiver exited"
                    await strategy.on_tick()
                    rows = await paper.open_orders()
                    assert {row.order_id for row in rows} <= set(strategy._active)
                    assert len({row.client_order_id for row in rows}) == len(rows)
                    if strategy.volatility is None:
                        assert rows == [], "unready trade/book window left quotes live"
                        counts["unready"] += 1
                    elif rows:
                        counts["quoting"] += 1
                    if second % 20 == 11:
                        assert strategy.volatility is None and rows == []
                        counts["missing_trade_retirement"] += 1
                    account = await paper.account()
                    size = account.positions.get("BTC", D(0))
                    buys = sum((row.quantity for row in rows if row.side == "buy"), D(0))
                    sells = sum((row.quantity for row in rows if row.side == "sell"), D(0))
                    assert size + buys <= D("0.25") and size - sells >= D("-0.25")
                    assert max(abs(size + buys), abs(size - sells)) * D(104) <= D(30)
                    assert all(row.quantity * row.price <= D(5) for row in rows)
                    assert account.equity > D(9995)
                assert counts["quoting"] > 300 and counts["missing_trade_retirement"] == 30
                assert len(paper.fills) > 20
                print(
                    f"SOAK book_and_trades: 600 simulated seconds, {dict(counts)}, "
                    f"fills={len(paper.fills)}"
                )
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await stream.aclose()
                await strategy.stop()
                await managed.close()
    assert await paper.open_orders() == []
    assert not fake._connections
    assert [row["body"]["type"] for row in fake.requests] == ["meta"]


@pytest.mark.parametrize("mutation", ["notional", "position", "duplicate", "loss", "rate"])
def test_independent_soak_oracle_rejects_unsafe_venue_boundary(mutation):
    wire = SoakWire(Clock())
    row = {"c": "0x" + "a" * 32, "b": True, "s": "0.01", "p": "99"}
    proposed = [row]
    wire.assert_limits()
    if mutation == "notional":
        proposed = [{**row, "s": "0.06"}]
    elif mutation == "position":
        proposed = [{**row, "s": "0.04"} for _ in range(8)]
    elif mutation == "duplicate":
        wire.accepted_ids.add(row["c"])
        envelope = {"action": {"type": "order", "orders": proposed}}
        with pytest.raises(AssertionError, match="duplicate"):
            wire.http(httpx.Request("POST", "https://offline.invalid/v1/exchange", json=envelope))
        return
    elif mutation == "loss":
        wire.ledger._cash[OWNER] = D(9990)
    else:
        wire.attempts = [0.0] * 601
    # The venue oracle works without any guard being present; it cannot certify
    # itself merely by reusing the production validation method under test.
    assert wire.guard is None
    with pytest.raises(AssertionError):
        wire.assert_limits(proposed)
