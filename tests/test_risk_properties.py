"""Independent venue-ledger invariants over generated strategy/fault histories."""

import asyncio
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from glyde_mm.models import (
    Account,
    Book,
    CancelResult,
    Fill,
    Level,
    OpenOrder,
    OrderRequest,
    OrderResult,
)
from glyde_mm.risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation
from glyde_mm.strategies import StrategyContext, create_strategy

ZERO = D(0)


class LedgerVenue:
    """Actual accepted orders are independent of the guard's reservations/read view."""

    def __init__(self, owner):
        self.owner = owner
        self.live = {}
        self.terminal = set()
        self.position = ZERO
        self.equity = D(1000)
        self.mode = "resting"
        self.cancel_confirmed = True
        self.unknown_cleanup_confirmed = True
        self.hidden = False
        self.serial = 0
        self.dispatches = []
        self.cancel_attempts = set()

    async def account(self):
        return Account(self.equity, {"BTC": self.position}, received_at=self.owner.now)

    async def open_orders(self):
        return [] if self.hidden else list(self.live.values())

    async def place_orders(self, orders):
        # Independent exposure oracle at the actual dispatch boundary.
        assert not self.owner.no_more_dispatch
        assert all(o.client_order_id not in self.owner.sent_ids for o in orders)
        assert len({o.client_order_id for o in orders}) == len(orders)
        for order in orders:
            assert order.price * order.quantity <= self.owner.limits.max_order_notional
        all_orders = [*self.live.values(), *orders]
        buys = sum((o.quantity for o in all_orders if o.side == "buy"), ZERO)
        sells = sum((o.quantity for o in all_orders if o.side == "sell"), ZERO)
        assert self.position + buys <= self.owner.limits.max_position
        assert self.position - sells >= -self.owner.limits.max_position
        self.owner.sent_ids.update(o.client_order_id for o in orders)
        self.dispatches.append(tuple(orders))
        results = []
        for order in orders:
            if self.mode == "rejected":
                results.append(OrderResult("rejected", client_order_id=order.client_order_id))
                continue
            self.serial += 1
            oid = str(self.serial)
            self.live[oid] = OpenOrder(
                order_id=oid,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                price=order.price,
                client_order_id=order.client_order_id,
            )
            results.append(OrderResult("resting", oid, order.client_order_id))
        if self.mode == "unknown":
            self.cancel_confirmed = self.unknown_cleanup_confirmed
            self.hidden = True
            self.owner.no_more_dispatch = True
            raise TimeoutError("accepted; acknowledgement lost")
        return results

    async def cancel_orders(self, requests):
        results = []
        for request in requests:
            ids = [
                oid
                for oid, row in self.live.items()
                if (
                    oid == request.order_id
                    or (request.client_order_id and row.client_order_id == request.client_order_id)
                )
            ]
            self.cancel_attempts.update(ids)
            if not self.cancel_confirmed:
                results.append(CancelResult("unknown", request.order_id, request.client_order_id))
                continue
            for oid in ids:
                self.terminal.add(oid)
                del self.live[oid]
            results.append(CancelResult("cancelled", request.order_id, request.client_order_id))
        return results

    async def cancel_all(self, symbol=None):
        # Like OneApiClient, discovery can only cancel orders present in its read.
        from glyde_mm.models import CancelRequest

        requests = [
            CancelRequest(row.symbol, order_id=row.order_id)
            for row in await self.open_orders()
            if symbol is None or row.symbol == symbol
        ]
        results = await self.cancel_orders(requests)
        if any(row.status != "cancelled" for row in results):
            raise RiskViolation("cancel_outcome_unknown")


class RiskHistory(RuleBasedStateMachine):
    def __init__(self, flavor):
        super().__init__()
        self.flavor = flavor
        self.loop = asyncio.new_event_loop()
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "risk.json"
        self.now = 0.0
        self.mid = D(100)
        self.sent_ids = set()
        self.no_more_dispatch = False
        self.fill_serial = 0
        self.intent_serial = 0
        self.venue = LedgerVenue(self)

    def run(self, awaitable):
        return self.loop.run_until_complete(awaitable)

    def make_client(self):
        self.guard = RiskGuard(
            self.limits,
            clock=lambda: self.now,
            wall_clock=lambda: 1_700_000_000 + self.now,
            state_path=self.path,
        )
        self.client = RiskManagedClient(self.venue, self.guard, cleanup_symbol="BTC")
        self.strategy = create_strategy(
            self.kind,
            StrategyContext(self.client, "BTC", D("0.01"), D("0.001"), clock=lambda: self.now),
            {"order_amount": "0.01", "order_refresh_time": 0.1, **self.options},
        )

    def observe(self, price=None):
        self.now += 1
        self.mid = self.mid if price is None else D(price)
        book = Book(
            "BTC",
            (Level(self.mid - D("0.5"), D(10)),),
            (Level(self.mid + D("0.5"), D(10)),),
            round((1_700_000_000 + self.now) * 1000),
            self.now,
        )
        self.guard.update_book(book)
        self.run(self.strategy.on_book(book))

    def tick(self):
        try:
            self.run(self.strategy.on_tick())
        except (RiskViolation, TimeoutError, ConnectionError, RuntimeError):
            assert self.guard.halted is not None or self.strategy.blocked

    @initialize(
        cap=st.integers(3, 30),
        notional=st.integers(2, 25),
    )
    def start(self, cap, notional):
        kind = self.flavor
        self.kind = "avellaneda_market_making" if kind == "avellaneda" else "pure_market_making"
        self.options = (
            {"volatility_samples": 2}
            if kind == "avellaneda"
            else {"bid_spread": "0.01", "ask_spread": "0.01"}
        )
        if kind == "levels":
            self.options.update(order_levels=3, order_level_spread="0.01")
        self.limits = RiskLimits(
            max_position=D(cap) / 100,
            max_order_notional=D(notional),
            max_daily_loss=D(5),
            max_orders_per_minute=6000,
        )
        self.make_client()
        for price in [100, 101, 100]:
            self.observe(price)
        self.tick()
        assert self.venue.dispatches  # Each strategy must actually quote, not pass vacuously.

    @rule(price=st.integers(60, 140))
    def quotes_and_price_moves(self, price):
        self.venue.mode = "resting"
        self.observe(price)
        self.tick()

    @precondition(lambda self: self.guard.halted is None)
    @rule(
        side=st.sampled_from(["buy", "sell"]), units=st.integers(1, 100), price=st.integers(1, 300)
    )
    def adversarial_quote_intent(self, side, units, price):
        self.observe()
        self.intent_serial += 1
        order = OrderRequest(
            "BTC",
            side,
            D(units) / 1000,
            D(price),
            client_order_id=f"0x{self.intent_serial:032x}",
        )
        try:
            self.run(self.client.place_orders([order]))
        except (RiskViolation, TimeoutError):
            assert self.guard.halted is not None

    @precondition(lambda self: bool(self.venue.live))
    @rule(index=st.integers(0, 20), quarters=st.integers(1, 4))
    def partial_or_complete_fill(self, index, quarters):
        oid = sorted(self.venue.live)[index % len(self.venue.live)]
        order = self.venue.live[oid]
        quantity = order.quantity * quarters / 4
        self.venue.position += quantity if order.side == "buy" else -quantity
        self.fill_serial += 1
        if quantity == order.quantity:
            self.venue.terminal.add(oid)
            del self.venue.live[oid]
        else:
            self.venue.live[oid] = replace(order, quantity=order.quantity - quantity)
        self.run(
            self.strategy.on_fill(
                Fill(
                    "BTC",
                    order.side,
                    quantity,
                    order.price,
                    oid,
                    client_order_id=order.client_order_id,
                    trade_id=str(self.fill_serial),
                )
            )
        )

    @precondition(lambda self: self.guard.halted is None and not self.strategy.blocked)
    @rule()
    def venue_refusals(self):
        self.venue.mode = "rejected"
        self.observe()
        self.tick()

    @precondition(lambda self: self.guard.halted is None and not self.strategy.blocked)
    @rule(cleanup_confirmed=st.booleans())
    def lost_acknowledgement(self, cleanup_confirmed):
        self.venue.unknown_cleanup_confirmed = cleanup_confirmed
        self.venue.mode = "unknown"
        self.observe()
        self.tick()

    @precondition(lambda self: bool(self.venue.live))
    @rule()
    def cancellation_refusal(self):
        self.venue.cancel_confirmed = False
        try:
            self.run(self.client.cancel_all("BTC"))
        except RiskViolation:
            self.no_more_dispatch = True
        else:
            assert not self.venue.live

    @precondition(lambda self: self.no_more_dispatch)
    @rule()
    def retry_original_id_after_halt(self):
        before = len(self.venue.dispatches)
        try:
            self.run(self.client.place_orders([self.venue.dispatches[-1][0]]))
        except RiskViolation:
            pass
        else:
            raise AssertionError("halted session allowed an original-ID retry")
        assert len(self.venue.dispatches) == before

    @rule(loss=st.integers(0, 10))
    def daily_loss_or_recovery(self, loss):
        self.venue.equity = D(1000 - loss)
        if loss >= 5:
            self.no_more_dispatch = True
        try:
            self.guard.update_account(self.run(self.venue.account()))
        except RiskViolation:
            pass
        self.observe()
        self.tick()

    @rule(seconds=st.integers(4, 20))
    def stale_gap_and_reconnect(self, seconds):
        self.now += seconds
        try:
            self.run(self.client.check_liveness())
        except RiskViolation:
            pass
        self.observe()
        self.tick()

    @rule()
    def restart_with_original_risk_file(self):
        self.make_client()
        for price in [100, 101, 100]:
            self.observe(price)
        self.tick()

    @rule()
    def cancel_all(self):
        self.venue.cancel_confirmed = True
        before = set(self.venue.live)
        self.venue.cancel_attempts.clear()
        self.run(self.client.cancel_all("BTC"))
        assert before <= self.venue.cancel_attempts
        assert not self.venue.live

    @invariant()
    def every_possible_live_fill_remains_within_position_limit(self):
        buys = sum((o.quantity for o in self.venue.live.values() if o.side == "buy"), ZERO)
        sells = sum((o.quantity for o in self.venue.live.values() if o.side == "sell"), ZERO)
        assert self.venue.position + buys <= self.limits.max_position
        assert self.venue.position - sells >= -self.limits.max_position
        assert all(
            row.quantity * row.price <= self.limits.max_order_notional
            for row in self.venue.live.values()
        )

    def teardown(self):
        self.venue.hidden = False
        self.venue.cancel_confirmed = True
        try:
            self.run(self.client.close())
            assert not self.venue.live
        finally:
            self.loop.close()
            self.directory.cleanup()


@pytest.mark.parametrize("kind", ["pmm", "levels", "avellaneda"])
def test_generated_risk_histories(kind):
    run_state_machine_as_test(
        lambda: RiskHistory(kind),
        settings=settings(
            max_examples=100, stateful_step_count=35, deadline=None, derandomize=True
        ),
    )


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("prior_restart", [False, True])
def test_unknown_ack_cleanup_uses_original_ids_even_when_discovery_lags(restart, prior_restart):
    history = RiskHistory("pmm")
    try:
        history.start(cap=10, notional=25)
        if prior_restart:
            history.restart_with_original_risk_file()
        history.lost_acknowledgement(cleanup_confirmed=False)
        assert history.no_more_dispatch and history.venue.live
        assert history.guard._execution["reason"] == "order_outcome_unknown"
        assert history.run(history.venue.open_orders()) == []
        before = len(history.venue.dispatches)
        if restart:
            history.restart_with_original_risk_file()
        history.cancel_all()
        history.observe()
        history.tick()
        assert len(history.venue.dispatches) == before
    finally:
        history.teardown()
