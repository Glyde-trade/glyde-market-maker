from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from glyde_mm.models import BOT_CLOID_PREFIX, OpenOrder, bot_client_order_id
from glyde_mm.risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation, StaleAccount


@dataclass(frozen=True)
class Level:
    price: D


@dataclass(frozen=True)
class Book:
    symbol: str = "BTC"
    bids: tuple = (Level(D("99")),)
    asks: tuple = (Level(D("101")),)
    timestamp_ms: int = 1_700_000_000_000
    received_at: float = 100.0


@dataclass(frozen=True)
class Account:
    equity: D = D("100")
    positions: dict = field(default_factory=dict)
    received_at: float = 100.0


@dataclass(frozen=True)
class Order:
    symbol: str = "BTC"
    side: str = "buy"
    quantity: D = D("0.04")
    price: D = D("99")
    client_order_id: str | None = None


@dataclass(frozen=True)
class Cancel:
    order_id: str | None = None
    client_order_id: str | None = None
    symbol: str = "BTC"


class FakeClient:
    def __init__(self):
        self.placed = []
        self.cancelled = []
        self.all_cancels = 0
        self.result = "resting"
        self.cancel_result = "cancelled"
        self.current_account = Account()
        self.orders = []

    async def account(self):
        return self.current_account

    async def open_orders(self):
        return self.orders

    async def place_orders(self, orders):
        self.placed.extend(orders)
        if self.result == "timeout":
            raise TimeoutError("fake exchange accepted but response was lost")
        return [
            SimpleNamespace(status=self.result, order_id=str(i), client_order_id=o.client_order_id)
            for i, o in enumerate(orders)
        ]

    async def cancel_orders(self, orders):
        self.cancelled.extend(orders)
        return [SimpleNamespace(status=self.cancel_result) for _ in orders]

    async def cancel_all(self, symbol=None):
        self.all_cancels += 1


def guard(state_path: Path | None = None):
    now = [100.0, 1_700_000_000.0]
    g = RiskGuard(
        RiskLimits(max_position=D("0.1"), max_order_notional=D("10")),
        clock=lambda: now[0],
        wall_clock=lambda: now[1],
        state_path=state_path,
    )
    g.update_book(Book())
    g.update_account(Account())
    return g, now


def test_limit_uses_market_reference_not_an_unrealistic_order_price():
    g, _ = guard()
    with pytest.raises(RiskViolation, match="max_order_notional"):
        g.validate_order("BTC", "buy", D("0.1"), D("1"))
    assert not g.pending


def test_pending_and_external_orders_count_without_buy_sell_netting():
    g, _ = guard()
    g.update_account(Account(positions={"BTC": D("0.02")}))
    g.reserve(Order(client_order_id="one"))
    g.set_open_orders([Order(side="sell", quantity=D("0.01"), client_order_id="external")])
    with pytest.raises(RiskViolation, match="max_position"):
        g.validate_order("BTC", "buy", D("0.05"), D("99"))
    g.validate_order("BTC", "sell", D("0.01"), D("99"))


def test_observed_order_and_pending_id_are_not_counted_twice():
    g, _ = guard()
    order = Order(client_order_id="one")
    g.reserve(order)
    g.set_open_orders([order])
    g.validate_order("BTC", "buy", D("0.04"), D("99"))
    with pytest.raises(RiskViolation, match="duplicate"):
        g.reserve(order)


@pytest.mark.parametrize(
    "side,quantity,price",
    [
        ("other", D("1"), D("1")),
        ("buy", D("NaN"), D("1")),
        ("sell", D("1"), D("Infinity")),
        ("buy", 0.1, D("1")),
    ],
)
def test_non_exact_or_non_finite_orders_refuse(side, quantity, price):
    g, _ = guard()
    with pytest.raises(RiskViolation, match="invalid_order"):
        g.validate_order("BTC", side, quantity, price)


def test_arrival_and_source_staleness_both_refuse():
    g, now = guard()
    now[0] += 4
    with pytest.raises(RiskViolation, match="stale_book"):
        g.validate_order("BTC", "buy", D("0.01"), D("99"))
    g.update_book(Book(timestamp_ms=1_700_000_000_001, received_at=104.0))
    now[1] += 10
    with pytest.raises(RiskViolation, match="stale_book_source"):
        g.validate_order("BTC", "buy", D("0.01"), D("99"))


def test_repeated_source_cut_does_not_renew_book_deadline():
    g, now = guard()
    now[0] += 4
    g.update_book(Book(received_at=104.0))
    with pytest.raises(RiskViolation, match="stale_book"):
        g.check_fresh("BTC")
    with pytest.raises(RiskViolation, match="regressed_book"):
        g.update_book(Book(timestamp_ms=1))


def test_stale_account_and_crossed_book_refuse():
    g, now = guard()
    now[0] += 11
    g.update_book(Book(timestamp_ms=1_700_000_000_001, received_at=111))
    with pytest.raises(StaleAccount, match="stale_account"):
        g.check_fresh("BTC")
    g.update_account(Account(received_at=111))
    g.update_book(Book(bids=(Level(D("102")),), timestamp_ms=1_700_000_000_002, received_at=111))
    with pytest.raises(RiskViolation, match="invalid_book"):
        g.check_fresh("BTC")


def test_daily_loss_persists_across_restart_and_does_not_clear_on_equity_rebound(tmp_path):
    path = tmp_path / "risk.json"
    g, now = guard(path)
    with pytest.raises(RiskViolation, match="daily_loss"):
        g.update_account(Account(equity=D("95")))
    asyncio.run(g.flush_state())
    restarted, _ = guard_restart(path, now)
    with pytest.raises(RiskViolation, match="daily_loss"):
        restarted.update_account(Account(equity=D("105")))
    now[1] += 86400
    restarted.update_account(Account(equity=D("105")))
    assert restarted.halted is None


def guard_restart(path, now):
    return RiskGuard(
        RiskLimits(max_position=D("0.1")),
        clock=lambda: now[0],
        wall_clock=lambda: now[1],
        state_path=path,
    ), now


def test_corrupt_persisted_state_refuses_startup(tmp_path):
    path = tmp_path / "risk.json"
    path.write_text("not json")
    with pytest.raises(RiskViolation, match="risk_state_unavailable"):
        guard(path)


def test_an_order_over_the_position_cap_is_dropped_and_the_rest_are_sent():
    async def scenario():
        g, _ = guard()
        raw = FakeClient()
        client = RiskManagedClient(raw, g)
        results = await client.place_orders([Order(quantity=D("0.06")), Order(quantity=D("0.06"))])
        # The batch is checked cumulatively before sending: the second buy would breach the cap.
        assert [r.status for r in results] == ["resting", "rejected"]
        assert results[1].error == "max_position"
        assert len(raw.placed) == 1 and raw.all_cancels == 0 and g.halted is None

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["timeout", "unknown"])
def test_unknown_send_retains_original_ids_and_never_resends(status):
    async def scenario():
        g, _ = guard()
        raw = FakeClient()
        raw.result = status
        raw.cancel_result = "unknown"
        client = RiskManagedClient(raw, g)
        with pytest.raises((RiskViolation, TimeoutError)):
            await client.place_orders([Order()])
        assert len(raw.placed) == 1 and raw.placed[0].client_order_id.startswith("0x")
        assert len(g.pending) == 1 and raw.all_cancels == 1
        with pytest.raises(RiskViolation):
            await client.place_orders([Order()])
        assert len(raw.placed) == 1

    asyncio.run(scenario())


def test_only_definitive_cancel_ack_releases_reservation():
    async def scenario():
        g, _ = guard()
        raw = FakeClient()
        client = RiskManagedClient(raw, g)
        await client.place_orders([Order(client_order_id="one")])
        raw.cancel_result = "rejected"
        with pytest.raises(RiskViolation, match="cancel_outcome_unknown"):
            await client.cancel_orders([Cancel(order_id="0")])
        assert "one" in g.pending
        assert g.halted == "cancel_outcome_unknown"

    asyncio.run(scenario())


def test_stale_quote_disconnect_and_exit_all_cancel():
    async def scenario():
        g, now = guard()
        raw = FakeClient()
        client = RiskManagedClient(raw, g)
        await client.place_orders([Order()])
        now[0] += 4
        with pytest.raises(RiskViolation):
            await client.check_liveness()
        assert raw.all_cancels == 1
        await client.on_disconnect()
        await client.close()
        assert raw.all_cancels == 3
        with pytest.raises(RiskViolation, match="client_closed"):
            await client.place_orders([Order()])

    asyncio.run(scenario())


def test_daily_loss_cancels_existing_quotes_before_new_dispatch():
    async def scenario():
        g, _ = guard()
        raw = FakeClient()
        client = RiskManagedClient(raw, g)
        await client.place_orders([Order()])
        raw.current_account = Account(equity=D("95"))
        with pytest.raises(RiskViolation, match="daily_loss"):
            await client.place_orders([Order()])
        assert len(raw.placed) == 1 and raw.all_cancels == 1

    asyncio.run(scenario())


def test_stop_during_pretrade_read_prevents_dispatch():
    async def scenario():
        stop = asyncio.Event()
        g, _ = guard()

        class StoppingClient(FakeClient):
            async def account(self):
                stop.set()
                return await super().account()

        raw = StoppingClient()
        client = RiskManagedClient(raw, g, stop_event=stop)
        with pytest.raises(RiskViolation, match="client_closed"):
            await client.place_orders([Order()])
        assert raw.placed == [] and not g.pending

    asyncio.run(scenario())


def test_expired_private_read_does_not_prevent_known_original_cancel():
    async def scenario():
        g, _ = guard()

        class ExpiredClient(FakeClient):
            async def cancel_all(self, symbol=None):
                self.all_cancels += 1
                raise RuntimeError("private read credential expired before discovery")

        raw = ExpiredClient()
        client = RiskManagedClient(raw, g)
        await client.place_orders([Order(client_order_id="known-original")])
        with pytest.raises(RiskViolation, match="cancel_outcome_unknown"):
            await client.close()
        assert len(raw.cancelled) == 1
        assert raw.cancelled[0].order_id == "0"
        assert raw.cancelled[0].client_order_id is None
        assert len(raw.placed) == 1
        assert "known-original" in g.pending

    asyncio.run(scenario())


def test_ambiguous_original_is_cancelled_by_its_same_cloid_before_discovery():
    async def scenario():
        g, _ = guard()
        raw = FakeClient()
        raw.result = "timeout"
        client = RiskManagedClient(raw, g)
        with pytest.raises(TimeoutError):
            await client.place_orders([Order(client_order_id="original-unknown")])
        assert raw.cancelled[0].client_order_id == "original-unknown"
        assert raw.cancelled[0].order_id is None
        assert len(raw.placed) == 1
        assert g.halted == "order_outcome_unknown"

    asyncio.run(scenario())


def test_confirmed_cleanup_does_not_repeat_known_cancellations():
    async def scenario():
        g, _ = guard()
        raw = FakeClient()
        client = RiskManagedClient(raw, g)
        await client.place_orders([Order(client_order_id="one")])
        await client.on_disconnect()
        await client.close()
        assert len(raw.cancelled) == 1
        assert not g.pending
        assert g.halted == "disconnected"

    asyncio.run(scenario())


def test_first_cancel_failure_still_attempts_each_other_original_once():
    async def scenario():
        g, _ = guard()

        class BrokenClient(FakeClient):
            async def cancel_orders(self, orders):
                results = []
                for order in orders:
                    self.cancelled.append(order)
                    if order.order_id == "0":
                        raise TimeoutError("first original cancel reply was lost")
                    results.append(SimpleNamespace(status="cancelled", order_id=order.order_id))
                return results

            async def cancel_all(self, symbol=None):
                self.all_cancels += 1
                raise RuntimeError("private read credential expired")

        raw = BrokenClient()
        client = RiskManagedClient(raw, g)
        await client.place_orders([Order(client_order_id="one"), Order(client_order_id="two")])
        with pytest.raises(RiskViolation, match="cancel_outcome_unknown"):
            await client.close()
        assert [row.order_id for row in raw.cancelled] == ["0", "1"]
        assert raw.all_cancels == 1
        assert len(raw.placed) == 2
        assert set(g.pending) == {"one", "two"}
        assert g.halted == "closed"

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["timeout", "unknown"])
def test_ambiguous_execution_survives_restart_and_day_rollover(tmp_path, status):
    async def scenario():
        path = tmp_path / "risk.json"
        g, now = guard(path)
        raw = FakeClient()
        raw.result, raw.cancel_result = status, "unknown"
        client = RiskManagedClient(raw, g)
        with pytest.raises((RiskViolation, TimeoutError)):
            await client.place_orders([Order(client_order_id="original-uncertain")])
        restarted, _ = guard_restart(path, now)
        assert restarted.halted == "order_outcome_unknown"
        now[1] += 86400
        with pytest.raises(RiskViolation, match="order_outcome_unknown"):
            restarted.update_account(Account())
        restarted_client = RiskManagedClient(raw, restarted)
        with pytest.raises(RiskViolation, match="order_outcome_unknown"):
            await restarted_client.place_orders([Order(client_order_id="replacement")])
        assert [row.client_order_id for row in raw.placed] == ["original-uncertain"]
        import json

        record = json.loads(path.read_text())["execution"]
        assert record["originals"] == [
            {"symbol": "BTC", "order_id": None, "client_order_id": "original-uncertain"}
        ]

    asyncio.run(scenario())


def test_intent_is_durable_before_transport_and_definite_reply_clears_it(tmp_path):
    async def scenario():
        path = tmp_path / "risk.json"
        g, now = guard(path)

        class InspectingClient(FakeClient):
            async def place_orders(self, orders):
                interrupted, _ = guard_restart(path, now)
                assert interrupted.halted == "order_outcome_unknown"
                return await super().place_orders(orders)

        raw = InspectingClient()
        await RiskManagedClient(raw, g).place_orders([Order(client_order_id="known")])
        await g.flush_state()
        restarted, _ = guard_restart(path, now)
        assert restarted.halted is None
        restarted.update_account(Account())

    asyncio.run(scenario())


def test_uncertain_cancel_survives_restart_with_original_identity(tmp_path):
    async def scenario():
        path = tmp_path / "risk.json"
        g, now = guard(path)
        raw = FakeClient()
        client = RiskManagedClient(raw, g)
        await client.place_orders([Order(client_order_id="one")])
        raw.cancel_result = "unknown"
        with pytest.raises(RiskViolation, match="cancel_outcome_unknown"):
            await client.cancel_orders([Cancel(order_id="0")])
        restarted, _ = guard_restart(path, now)
        assert restarted.halted == "cancel_outcome_unknown"
        import json

        assert json.loads(path.read_text())["execution"]["originals"] == [
            {"symbol": "BTC", "order_id": "0", "client_order_id": None}
        ]

    asyncio.run(scenario())


def test_account_check_waits_out_an_in_flight_order_instead_of_latching_it(monkeypatch):
    async def scenario():
        g, _ = guard()
        raw = FakeClient()
        client = RiskManagedClient(raw, g)
        flushing, release = asyncio.Event(), asyncio.Event()

        async def slow_flush():
            flushing.set()
            await release.wait()

        monkeypatch.setattr(g, "flush_state", slow_flush)
        placing = asyncio.create_task(client.place_orders([Order()]))
        await flushing.wait()
        observing = asyncio.create_task(client.observe_account())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(placing, observing)
        assert len(raw.placed) == 1 and g.halted is None

    asyncio.run(scenario())


def test_state_write_failure_prevents_order_submission(tmp_path, monkeypatch):
    async def scenario():
        path = tmp_path / "risk.json"
        g, _ = guard(path)
        raw = FakeClient()

        def no_space(*_):
            raise OSError("state storage unavailable")

        monkeypatch.setattr("glyde_mm.risk_state.os.replace", no_space)
        with pytest.raises(RiskViolation):
            await RiskManagedClient(raw, g).place_orders([Order(client_order_id="never-sent")])
        assert not raw.placed
        assert g.halted == "risk_state_unavailable"

    asyncio.run(scenario())


def test_exit_before_account_baseline_keeps_readable_state(tmp_path):
    async def scenario():
        path = tmp_path / "risk.json"
        g = RiskGuard(RiskLimits(), state_path=path)
        await RiskManagedClient(FakeClient(), g).close()
        restarted = RiskGuard(RiskLimits(), state_path=path)
        assert restarted.halted is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "execution",
    [
        "not-a-record",
        {},
        {"reason": "approved", "originals": []},
        {"reason": "order_outcome_unknown", "originals": [{"symbol": "BTC"}]},
    ],
)
def test_malformed_execution_state_refuses_restart(tmp_path, execution):
    import json

    path = tmp_path / "risk.json"
    initial, _ = guard(path)
    asyncio.run(initial.close_state())
    data = json.loads(path.read_text())
    data["execution"] = execution
    path.write_text(json.dumps(data))
    with pytest.raises(RiskViolation, match="risk_state_unavailable"):
        guard(path)


def test_failed_state_clear_after_reply_retains_durable_original(tmp_path, monkeypatch):
    async def scenario():
        path = tmp_path / "risk.json"
        g, now = guard(path)

        class LostStateClient(FakeClient):
            async def place_orders(self, orders):
                result = await super().place_orders(orders)

                def no_space(*_):
                    raise OSError("state clear unavailable")

                monkeypatch.setattr("glyde_mm.risk_state.os.replace", no_space)
                return result

        raw = LostStateClient()
        with pytest.raises(RiskViolation, match="risk_state_unavailable"):
            await RiskManagedClient(raw, g).place_orders([Order(client_order_id="original")])
            await g.flush_state()
        assert len(raw.placed) == 1
        restarted, _ = guard_restart(path, now)
        assert restarted.halted == "order_outcome_unknown"

    asyncio.run(scenario())


def test_order_rate_window_counts_orders_and_survives_restart(tmp_path):
    path = tmp_path / "risk.json"
    g, now = guard(path)
    g.limits = RiskLimits(max_orders_per_minute=3)
    g.consume_order_rate(2)
    now[1] += 30
    now[0] += 30
    g.consume_order_rate(1)
    asyncio.run(g.flush_state())
    restored = RiskGuard(g.limits, wall_clock=lambda: now[1], state_path=path)
    with pytest.raises(RiskViolation, match="order_rate_limit"):
        restored.consume_order_rate(1)
    # At exactly 60 seconds only the first batch expires; retained attempts still count.
    now[1] += 30
    restored = RiskGuard(g.limits, wall_clock=lambda: now[1], state_path=path)
    restored.consume_order_rate(2)
    with pytest.raises(RiskViolation, match="order_rate_limit"):
        restored.consume_order_rate(1)


async def test_order_rate_cap_prevents_whole_batch_dispatch_and_allows_cancel():
    g, _ = guard()
    g.limits = RiskLimits(max_position=D(1), max_orders_per_minute=1)
    raw = FakeClient()
    managed = RiskManagedClient(raw, g)
    with pytest.raises(RiskViolation, match="order_rate_limit"):
        await managed.place_orders([Order(quantity=D("0.01")), Order(quantity=D("0.01"))])
    assert raw.placed == []
    assert g.pending == {}
    assert g.halted == "order_rate_limit"
    assert raw.all_cancels == 1
    await managed.cancel_orders([Cancel(order_id="known")])
    assert raw.cancelled[-1].order_id == "known"


async def test_rejected_orders_and_cancellations_do_not_refund_rate():
    g, _ = guard()
    g.limits = RiskLimits(max_position=D(1), max_orders_per_minute=1)
    raw = FakeClient()
    raw.result = "rejected"
    managed = RiskManagedClient(raw, g)
    await managed.place_orders([Order(quantity=D("0.01"))])
    await managed.cancel_all()
    with pytest.raises(RiskViolation, match="order_rate_limit"):
        await managed.place_orders([Order(quantity=D("0.01"))])
    assert len(raw.placed) == 1


async def test_unknown_order_still_consumes_rate_and_preserves_execution_quarantine(tmp_path):
    path = tmp_path / "risk.json"
    g, _ = guard(path)
    raw = FakeClient()
    raw.result = "timeout"
    with pytest.raises(TimeoutError):
        await RiskManagedClient(raw, g).place_orders([Order(quantity=D("0.01"))])
    restored = RiskGuard(g.limits, wall_clock=lambda: 1_700_000_000.0, state_path=path)
    assert len(restored._order_attempts) == 1
    assert restored.halted == "order_outcome_unknown"


def test_order_rate_refuses_backwards_clock_and_invalid_saved_clock(tmp_path):
    path = tmp_path / "risk.json"
    g, now = guard(path)
    g.consume_order_rate(1)
    asyncio.run(g.flush_state())
    now[1] -= 1
    with pytest.raises(RiskViolation, match="order_rate_clock"):
        g.consume_order_rate(1)
    with pytest.raises(RiskViolation, match="risk_state_unavailable"):
        RiskGuard(g.limits, wall_clock=lambda: now[1], state_path=path)


@pytest.mark.parametrize("cap", [True, 0, -1, 1.5, 6001, "60"])
def test_invalid_order_rate_caps_refuse(cap):
    with pytest.raises(ValueError, match="max_orders_per_minute"):
        RiskLimits(max_orders_per_minute=cap)


def test_wall_clock_jump_does_not_release_in_process_rate_budget():
    g, now = guard()
    g.limits = RiskLimits(max_orders_per_minute=1)
    g.consume_order_rate(1)
    now[1] += 3600
    with pytest.raises(RiskViolation, match="order_rate_limit"):
        g.consume_order_rate(1)


async def test_rate_timestamp_is_consumed_after_private_snapshot_awaits():
    g, now = guard()
    raw = FakeClient()
    original = raw.open_orders

    async def delayed_snapshot():
        now[0] += 1
        now[1] += 1
        return await original()

    raw.open_orders = delayed_snapshot
    await RiskManagedClient(raw, g).place_orders([Order(quantity=D("0.01"))])
    assert g._order_attempts == [1_700_000_002.0]


async def test_disk_and_transport_wait_do_not_expire_dispatched_rate_early():
    g, now = guard()
    g.limits = RiskLimits(max_position=D(1), max_orders_per_minute=1)

    class DelayedReply(FakeClient):
        async def place_orders(self, orders):
            now[0] += 30
            now[1] += 30
            return await super().place_orders(orders)

    await RiskManagedClient(DelayedReply(), g).place_orders([Order(quantity=D("0.01"))])
    assert g._order_attempts == [now[1]]
    now[0] += 31
    now[1] += 31
    with pytest.raises(RiskViolation, match="order_rate_limit"):
        g.consume_order_rate(1)


def test_cleanup_cancels_only_bot_orders_and_keeps_foreign_exposure():
    async def scenario():
        g, _ = guard()
        raw = FakeClient()
        manual = OpenOrder("77", "BTC", "sell", D("0.01"), D("120"))
        leftover = OpenOrder("78", "BTC", "buy", D("0.01"), D("90"), bot_client_order_id())
        raw.orders = [manual, leftover]
        client = RiskManagedClient(raw, g, cleanup_symbol="BTC")
        await client.place_orders([Order()])
        placed = raw.placed[0].client_order_id
        assert placed.startswith(BOT_CLOID_PREFIX) and len(placed) == 34
        await client.close()
        cancelled = {(row.order_id, row.client_order_id) for row in raw.cancelled}
        assert cancelled == {("78", None), ("0", None)}
        assert g.open_orders == [manual] and not g.pending

    asyncio.run(scenario())


def test_daily_loss_shrinks_the_position_cap_until_only_reducing_orders_fit():
    g, _ = guard()  # Day starts at equity 100; max_daily_loss 5, max_position 0.1.
    assert g.loss_headroom() == 1
    g.update_account(Account(equity=D("97.5")))  # Half the loss budget used: full cap.
    assert g.loss_headroom() == 1
    g.update_account(Account(equity=D("96.25"), positions={"BTC": D("0.04")}))
    assert g.loss_headroom() == D("0.5")  # Cap is now 0.05.
    with pytest.raises(RiskViolation, match="max_position"):
        g.validate_order("BTC", "buy", D("0.04"), D("99"))
    g.validate_order("BTC", "sell", D("0.04"), D("101"))
    g.update_account(Account(equity=D("95.01"), positions={"BTC": D("0.04")}))
    with pytest.raises(RiskViolation, match="max_position"):
        g.validate_order("BTC", "buy", D("0.001"), D("99"))
    g.validate_order("BTC", "sell", D("0.04"), D("101"))  # Flattening is always allowed.
