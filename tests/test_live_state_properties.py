"""Generated sequenced-wire histories, checked at the real risk/quote boundary."""

import asyncio
import socket
from dataclasses import replace
from decimal import Decimal as D
from decimal import localcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from live_wire import batch, owner_roots

from glyde_mm import live_state
from glyde_mm.client import OneApiError, account_from_wire, book_from_wire
from glyde_mm.config import Config, RiskConfig
from glyde_mm.live_state import LiveStateClient
from glyde_mm.models import Book, CancelResult, Level, OpenOrder, OrderRequest, OrderResult, Trade
from glyde_mm.risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation
from glyde_mm.strategies import AvellanedaConfig, StrategyContext, create_strategy
from glyde_mm.strategies.avellaneda_market_making import AvellanedaMarketMakingStrategy

OWNER = "0x" + "1" * 40
ZERO = D(0)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offline histories cannot open sockets")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


class Ledger:
    """Authoritative economics and dispatch checks do not read cache internals."""

    def __init__(self, history):
        self.history = history
        self.orders = {}
        self.fills = []
        self.position = ZERO
        self.serial = 0
        self.dispatches = 0

    async def place_orders(self, orders):
        h = self.history
        assert not h.unsafe
        assert h.book_ready and h.owner_ready
        assert h.now - h.book_received <= 5
        assert -1 <= h.wall() - h.book_stamp / 1000 <= 5
        assert h.now - h.owner_received <= 10
        self.dispatches += 1
        replies = []
        for row in orders:
            self.serial += 1
            oid = str(self.serial)
            self.orders[oid] = OpenOrder(
                oid, row.symbol, row.side, row.quantity, row.price, row.client_order_id
            )
            replies.append(OrderResult("resting", oid, row.client_order_id))
        h.publish_owner()
        return replies

    async def cancel_orders(self, requests):
        for request in requests:
            for oid, row in list(self.orders.items()):
                if oid == request.order_id or (
                    request.client_order_id and request.client_order_id == row.client_order_id
                ):
                    del self.orders[oid]
        if not self.history.unsafe:
            self.history.publish_owner()
        return [CancelResult("cancelled", r.order_id, r.client_order_id) for r in requests]

    def fill(self, selector, fraction):
        if not self.orders:
            return
        oid = sorted(self.orders)[selector % len(self.orders)]
        order = self.orders[oid]
        quantity = order.quantity * fraction
        self.position += quantity if order.side == "buy" else -quantity
        self.fills.append(
            {
                "oid": int(oid),
                "tid": len(self.fills) + 1,
                "coin": "BTC",
                "side": "B" if order.side == "buy" else "A",
                "sz": str(quantity),
                "px": str(order.price),
                "fee": "0",
                "cloid": order.client_order_id,
            }
        )
        if quantity == order.quantity:
            del self.orders[oid]
        else:
            self.orders[oid] = replace(order, quantity=order.quantity - quantity)


class CacheHistory(RuleBasedStateMachine):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self.loop = asyncio.new_event_loop()
        self.now = 100.0
        self.venue = Ledger(self)
        self.connection = 0
        self.callbacks = set()
        self.expected_callbacks = set()
        self.patches = [
            patch.object(
                live_state, "time", SimpleNamespace(monotonic=lambda: self.now, time=self.wall)
            ),
            patch.object(
                live_state,
                "account_from_wire",
                lambda data: replace(account_from_wire(data), received_at=self.now),
            ),
        ]
        for active in self.patches:
            active.start()
        self.connect()

    def wall(self):
        return 1_700_000_000 + self.now

    def run(self, awaitable):
        return self.loop.run_until_complete(awaitable)

    def connect(self):
        self.connection += 1
        self.cache = LiveStateClient(
            self.venue, Config(mode="live", owner=OWNER, risk=RiskConfig())
        )
        self.owner_seq = self.book_seq = 0
        self.unsafe = self.owner_ready = self.book_ready = False
        self.owner_received = self.book_received = self.book_stamp = 0
        self.baseline_fills = {(str(f["oid"]), str(f["tid"])) for f in self.venue.fills}
        self.expected_callbacks = set(self.callbacks)
        self.guard = RiskGuard(
            RiskLimits(max_position=D(100), max_order_notional=D(25), max_orders_per_minute=6000),
            clock=lambda: self.now,
            wall_clock=self.wall,
        )
        self.managed = RiskManagedClient(self.cache, self.guard, cleanup_symbol="BTC")
        name = "avellaneda_market_making" if self.kind == "avellaneda" else "pure_market_making"
        options = {"order_amount": "0.01", "order_refresh_time": 0.1}
        options.update({"volatility_samples": 2} if self.kind == "avellaneda" else {})
        if self.kind == "levels":
            options.update(order_levels=3, order_level_spread="0.01")
        self.strategy = create_strategy(
            name,
            StrategyContext(self.managed, "BTC", D("0.01"), D("0.001"), clock=lambda: self.now),
            options,
        )

    def owner_frames(self, *, snapshot=False, reverse=False):
        previous = self.owner_seq
        self.owner_seq += 1
        roots = owner_roots(
            OWNER,
            position=str(self.venue.position),
            stamp=round(self.wall() * 1000),
            fills=list(reversed(self.venue.fills)) if reverse else self.venue.fills,
            orders=[
                {
                    "oid": int(o.order_id),
                    "coin": "BTC",
                    "side": "B" if o.side == "buy" else "A",
                    "sz": str(o.quantity),
                    "limitPx": str(o.price),
                    "cloid": o.client_order_id,
                }
                for o in self.venue.orders.values()
            ],
        )
        roots["userFills"]["isSnapshot"] = snapshot
        return batch(
            "mm-owner",
            roots,
            owner=OWNER,
            sequence=self.owner_seq,
            previous=None if snapshot or not previous else previous,
            kind="snapshot" if snapshot else None,
        )

    def consume(self, frames):
        try:
            for frame in frames:
                self.cache.consume(frame)
        except OneApiError:
            self.unsafe = True
            return False
        return True

    def publish_owner(self, *, snapshot=False, reverse=False):
        frames = self.owner_frames(snapshot=snapshot, reverse=reverse)
        initial = not self.owner_ready
        if self.consume(frames):
            self.owner_ready = True
            self.owner_received = self.now
            if not initial:
                self.expected_callbacks.update(
                    (str(f["oid"]), str(f["tid"]))
                    for f in self.venue.fills
                    if (str(f["oid"]), str(f["tid"])) not in self.baseline_fills
                )

    def publish_book(self, mid=100, *, source_age=0):
        previous = self.book_seq
        self.book_seq += 1
        stamp = round((self.wall() - source_age) * 1000)
        data = {
            "coin": "BTC",
            "time": stamp,
            "levels": [[{"px": str(mid - 1), "sz": "10"}], [{"px": str(mid + 1), "sz": "10"}]],
        }
        if self.consume(
            batch(
                "mm-book-0",
                {"l2Book": data},
                sequence=self.book_seq,
                previous=previous if previous else None,
            )
        ):
            self.book_ready, self.book_received, self.book_stamp = True, self.now, stamp

    def drain(self):
        while not self.cache._events.empty():
            frame = self.cache._events.get_nowait()
            if frame is None or self.unsafe:
                continue
            if frame["channel"] == "l2Book":
                current = replace(book_from_wire(frame["data"]), received_at=self.now)
                try:
                    self.guard.update_book(current)
                except RiskViolation:
                    self.unsafe = True
                    self.cache.invalidate("runner rejected a regressed book")
                    continue
                self.run(self.strategy.on_book(current))
            else:
                for fill in frame["fills"]:
                    identity = fill.order_id, fill.trade_id
                    assert identity not in self.callbacks
                    self.callbacks.add(identity)
                    self.run(self.strategy.on_fill(fill))
        if not self.unsafe:
            assert self.callbacks == self.expected_callbacks

    def tick(self):
        before = self.venue.dispatches
        prohibited = (
            self.unsafe
            or not self.owner_ready
            or not self.book_ready
            or (self.now - self.book_received > 5 or self.wall() - self.book_stamp / 1000 > 5)
        )
        try:
            self.run(self.strategy.on_tick())
        except (OneApiError, RiskViolation):
            pass
        if prohibited:
            assert self.venue.dispatches == before
        self.drain()

    @initialize()
    def seed_and_quote(self):
        self.publish_owner(snapshot=True)
        for mid in (100, 101, 100):
            self.now += 1
            self.publish_book(mid)
            self.drain()
        self.tick()
        assert self.venue.dispatches > 0

    @rule(mid=st.integers(60, 140), snapshot=st.booleans())
    def fresh_delta(self, mid, snapshot):
        self.now += 0.25
        self.publish_owner(snapshot=snapshot)
        self.publish_book(mid)
        self.drain()
        self.tick()

    @rule(
        selector=st.integers(0, 20),
        partial=st.booleans(),
        reverse=st.booleans(),
        snapshot=st.booleans(),
    )
    def own_fill(self, selector, partial, reverse, snapshot):
        self.venue.fill(selector, D("0.5") if partial else D(1))
        self.publish_owner(snapshot=snapshot, reverse=reverse)
        self.drain()
        self.tick()

    @rule(reverse=st.booleans())
    def duplicate_fills(self, reverse):
        self.publish_owner(reverse=reverse)
        self.drain()

    @rule(group=st.sampled_from(["owner", "book"]))
    def gap(self, group):
        self.unsafe = True
        if group == "owner":
            self.owner_seq += 1
            self.publish_owner()
        else:
            self.book_seq += 1
            self.publish_book()
        with pytest.raises(OneApiError):
            self.cache.check()
        self.tick()

    @rule(seconds=st.integers(6, 20), refresh_owner=st.booleans())
    def stale(self, seconds, refresh_owner):
        self.now += seconds
        if refresh_owner:
            self.publish_owner()
            self.drain()
        self.tick()

    @precondition(lambda self: self.owner_ready and not self.unsafe and self.guard.halted is None)
    @rule(parts=st.integers(1, 4), expire=st.booleans())
    def incomplete_owner_batch(self, parts, expire):
        before = self.cache._account
        frames = self.owner_frames()
        self.consume(frames[:parts])
        assert self.cache._account == before
        dispatches = self.venue.dispatches

        async def interleave():
            task = asyncio.create_task(
                self.managed.place_orders([OrderRequest("BTC", "buy", D("0.001"), D(99))])
            )
            await asyncio.sleep(0)
            assert not task.done() and self.venue.dispatches == dispatches
            if expire:
                self.now += 3
                with pytest.raises(OneApiError, match="incomplete"):
                    self.cache.check()
                self.unsafe = True
            self.consume(frames[parts:])
            if not self.unsafe:
                self.owner_received = self.now
            try:
                await task
            except (OneApiError, RiskViolation):
                assert self.unsafe or self.guard.halted is not None

        self.run(interleave())
        if expire:
            assert self.venue.dispatches == dispatches
        self.drain()

    @rule(seconds=st.integers(6, 20))
    def stale_source_on_fresh_delivery(self, seconds):
        self.now += seconds
        self.publish_owner()
        self.publish_book(source_age=seconds)
        self.drain()
        self.tick()

    @rule(baseline=st.booleans())
    def reconnect(self, baseline):
        self.cache.invalidate("disconnected")
        self.unsafe = True
        self.tick()
        # A new connection requires independent snapshots, never inherited cache authority.
        self.venue.orders.clear()
        self.connect()
        if baseline:
            self.publish_owner(snapshot=True)
            self.publish_book()
            self.drain()
        else:
            self.unsafe = True
            self.owner_seq = 1
            self.publish_owner()
        self.tick()

    @invariant()
    def position_is_snapshot_authority_not_sum_of_notifications(self):
        if self.owner_ready and not self.unsafe:
            try:
                account = self.run(self.cache.account())
            except OneApiError:
                assert self.cache._failure is not None
                self.unsafe = True
                return
            assert account.positions.get("BTC", ZERO) == self.venue.position
            expected = sum(
                (D(f["sz"]) * (1 if f["side"] == "B" else -1) for f in self.venue.fills), ZERO
            )
            assert account.positions.get("BTC", ZERO) == expected

    def teardown(self):
        for active in reversed(self.patches):
            active.stop()
        self.loop.close()


@pytest.mark.parametrize("kind", ["pmm", "levels", "avellaneda"])
def test_generated_cache_quote_histories(kind):
    run_state_machine_as_test(
        lambda: CacheHistory(kind),
        settings=settings(
            max_examples=100, stateful_step_count=30, deadline=None, derandomize=True
        ),
    )


EVENTS = st.lists(
    st.tuples(
        st.sampled_from(["book", "trade", "duplicate", "gap"]),
        st.integers(1, 10000),
        st.integers(1, 10000),
        st.integers(0, 3),
    ),
    min_size=1,
    max_size=80,
)


@given(events=EVENTS)
@example(events=[("book", 100, 1, 0), ("trade", 100, 1, 0)] * 4)
@example(
    events=[
        item
        for price in (100, 101, 100, 102)
        for item in (("book", price, 1, 1), ("trade", price, 1, 0))
    ]
)
@settings(max_examples=150, deadline=None, derandomize=True)
def test_sparse_avellaneda_stream_is_finite_and_bounded(events):
    async def check():
        strategy = AvellanedaMarketMakingStrategy(
            StrategyContext(None, "BTC", D("0.01"), D("0.001")),
            AvellanedaConfig(volatility_samples=2, volatility_source="book_and_trades"),
        )
        timestamp = 0
        previous = None
        observations = {}
        bucket = None
        closed = []
        serial = 0
        for kind, price, quantity, advance in events:
            timestamp += advance * 1000 + (5000 if kind == "gap" else 0)
            duplicate = kind == "duplicate" and previous is not None
            source_time = previous.timestamp_ms if duplicate else timestamp
            current = source_time // 1000
            if bucket is None or current >= bucket:
                if bucket is not None and current > bucket:
                    rows = observations.get(bucket, {})
                    trades = list(rows.get("trades", {}).values())
                    if "mid" in rows and trades and current == bucket + 1:
                        vwap = sum((t.price * t.quantity for t in trades), ZERO) / sum(
                            (t.quantity for t in trades), ZERO
                        )
                        closed.append((rows["mid"] + vwap) / 2)
                        closed = closed[-3:]
                    else:
                        closed.clear()
                bucket = current
                rows = observations.setdefault(current, {"trades": {}})
                if kind == "book":
                    rows["mid"] = D(price)
                elif duplicate:
                    rows["trades"][previous.trade_id] = previous
                else:
                    rows["trades"][str(serial)] = Trade(
                        "BTC", "buy", D(quantity) / 100, D(price), timestamp, str(serial)
                    )
            if kind == "book":
                mid = D(price)
                await strategy.on_book(
                    Book(
                        "BTC",
                        (Level(mid - D("0.1"), D(1)),),
                        (Level(mid + D("0.1"), D(1)),),
                        timestamp,
                    )
                )
            elif duplicate:
                await strategy.on_trade(previous)
            else:
                previous = Trade("BTC", "buy", D(quantity) / 100, D(price), timestamp, str(serial))
                serial += 1
                await strategy.on_trade(previous)
            with localcontext() as context:
                context.prec = 50
                expected = (
                    None
                    if len(closed) != 3
                    else (((closed[1] - closed[0]) ** 2 + (closed[2] - closed[1]) ** 2) / 2).sqrt()
                )
            value = strategy.volatility
            assert value == (expected if expected else None)
            assert value is None or value.is_finite() and ZERO < value <= D(10000)
            estimator = strategy._stream_volatility
            assert len(estimator.prices) <= 3 and len(estimator.trades) <= 4096
            if value is not None:
                assert len(estimator.prices) == 3
                if strategy.book is not None:
                    from glyde_mm.models import Account

                    reservation, spread = strategy.model_prices(strategy.book, Account(D(1000)))
                    assert reservation.is_finite() and spread.is_finite() and spread > 0

    asyncio.run(check())
