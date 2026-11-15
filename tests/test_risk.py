from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from glyde_mm.risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation


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
    with pytest.raises(RiskViolation, match="stale_account"):
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


def test_entire_batch_is_checked_before_any_order_is_sent():
    async def scenario():
        g, _ = guard()
        raw = FakeClient()
        client = RiskManagedClient(raw, g)
        with pytest.raises(RiskViolation, match="max_position"):
            await client.place_orders([Order(quantity=D("0.06")), Order(quantity=D("0.06"))])
        assert raw.placed == [] and g.pending == {}
        assert raw.all_cancels == 1

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


def test_state_write_failure_prevents_order_submission(tmp_path, monkeypatch):
    async def scenario():
        path = tmp_path / "risk.json"
        g, _ = guard(path)
        raw = FakeClient()

        def no_space(*_):
            raise OSError("state storage unavailable")

        monkeypatch.setattr("glyde_mm.risk.os.replace", no_space)
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
    guard(path)
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

                monkeypatch.setattr("glyde_mm.risk.os.replace", no_space)
                return result

        raw = LostStateClient()
        with pytest.raises(RiskViolation, match="risk_state_unavailable"):
            await RiskManagedClient(raw, g).place_orders([Order(client_order_id="original")])
        assert len(raw.placed) == 1
        restarted, _ = guard_restart(path, now)
        assert restarted.halted == "order_outcome_unknown"

    asyncio.run(scenario())
