from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest

from glyde_mm.config import load_config
from glyde_mm.models import Account, Book, CancelResult, Level, OrderResult
from glyde_mm.risk import RiskGuard, RiskLimits, RiskManagedClient
from glyde_mm.strategies import InventorySkewedConfig, InventorySkewedStrategy, StrategyContext
from glyde_mm.strategies.factory import create_strategy


def book(mid, time=2000):
    return Book("BTC", (Level(D(mid) - 1, D(10)),), (Level(D(mid) + 1, D(10)),), time, time / 1000)


class Client:
    def __init__(self, position=D(1)):
        self.position = position
        self.calls = []
        self.placed = []

    async def account(self):
        return Account(D(1000), {"BTC": self.position}, received_at=2)

    async def open_orders(self):
        return []

    async def place_orders(self, orders):
        self.calls.append("place")
        self.placed.append(orders)
        return [OrderResult("resting", str(i), row.client_order_id) for i, row in enumerate(orders)]

    async def cancel_orders(self, orders):
        self.calls.append("cancel")
        return [CancelResult("cancelled", row.order_id) for row in orders]

    async def cancel_all(self, symbol=None):
        self.calls.append("all")


def strategy(client=None, **options):
    return InventorySkewedStrategy(
        StrategyContext(client or Client(), "BTC", D("0.01"), D("0.001"), clock=lambda: 2),
        InventorySkewedConfig(
            order_amount=D(1), volatility_samples=2, order_amount_shape_factor=D(2), **options
        ),
    )


async def warmup(selected):
    for mid, timestamp in ((100, 0), (101, 1000), (100, 2000)):
        await selected.on_book(book(mid, timestamp))


@pytest.mark.parametrize(
    "position,quantities",
    [(D(1), [D("0.818"), D(1)]), (D(-1), [D(1), D("0.818")]), (D(0), [D(1), D(1)])],
)
async def test_eta_golden_shrinks_only_inventory_increasing_side(position, quantities):
    selected = strategy()
    await warmup(selected)
    orders = selected.quotes(book(100), Account(D(1000), {"BTC": position}))
    assert [row.quantity for row in orders] == quantities
    assert all(row.time_in_force == "Alo" for row in orders)


async def test_target_and_account_scale_define_signed_exposure_not_spot_balance():
    selected = strategy(inventory_target_position=D(2))
    await warmup(selected)
    at_target = Account(D(1000), {"BTC": D(2)})
    assert [row.quantity for row in selected.quotes(book(100), at_target)] == [D(1), D(1)]
    below = replace(at_target, positions={"BTC": D(1)})
    assert [row.quantity for row in selected.quotes(book(100), below)] == [D(1), D("0.818")]
    scaled = strategy(inventory_target_position=D(4))
    await warmup(scaled)
    assert selected.quotes(book(100), below) == scaled.quotes(
        book(100), Account(D(2000), {"BTC": D(2)})
    )


async def test_warmup_gap_and_subminimum_quotes_do_not_fabricate_orders():
    selected = strategy()
    assert not selected.quotes(book(100), Account(D(1000)))
    await warmup(selected)
    selected.context = replace(selected.context, min_notional=D(90))
    orders = selected.quotes(book(100), Account(D(1000), {"BTC": D(1)}))
    assert [row.side for row in orders] == ["sell"]
    await selected.on_book(book(100, 5000))
    assert not selected.quotes(book(100, 5000), Account(D(1000)))


async def test_fake_client_lifecycle_uses_actual_risk_boundary_before_dispatch():
    client = Client()
    guard = RiskGuard(
        RiskLimits(max_position=D(5), max_order_notional=D(500)),
        clock=lambda: 2,
        wall_clock=lambda: 2,
    )
    guard.update_book(book(100))
    managed = RiskManagedClient(client, guard)
    selected = strategy(managed, order_refresh_time=1)
    await warmup(selected)
    await selected.on_tick(2)
    assert client.calls == ["place"]
    assert [row.quantity for row in client.placed[0]] == [D("0.818"), D(1)]
    await selected.on_tick(3)
    assert client.calls == ["place", "cancel", "place"]
    await selected.stop()
    assert client.calls[-1] == "all"

    refusing = Client()
    guard = RiskGuard(
        RiskLimits(max_position=D("0.1"), max_order_notional=D(500)),
        clock=lambda: 2,
        wall_clock=lambda: 2,
    )
    guard.update_book(book(100))
    selected = strategy(RiskManagedClient(refusing, guard))
    await warmup(selected)
    await selected.on_tick(2)
    # A long of 1 over a 0.1 cap: bids are dropped before dispatch, sells that reduce the
    # position may go out but never past -0.1, and the strategy stays live.
    sent = [row for batch in refusing.placed for row in batch]
    assert not any(row.side == "buy" for row in sent)
    assert D(1) - sum((row.quantity for row in sent), D(0)) >= D("-0.1")
    assert not selected.blocked and guard.halted is None


@pytest.mark.parametrize("eta", [D(-1), D(101), D("NaN"), D("Infinity"), True])
def test_invalid_shape_factor_refuses(eta):
    with pytest.raises(ValueError):
        InventorySkewedConfig(order_amount_shape_factor=eta)


def test_paper_example_and_factory_preserve_exact_shape_factor():
    from pathlib import Path

    config = load_config(Path(__file__).parents[1] / "examples/inventory-skewed-paper.yaml")
    selected = create_strategy(config.strategy, strategy().context, config.strategy_config)
    assert isinstance(selected, InventorySkewedStrategy)
    assert selected.inventory_config.order_amount_shape_factor == D(2)
    assert config.mode == "paper"


async def test_zero_shape_factor_preserves_base_avellaneda_sizes():
    selected = InventorySkewedStrategy(
        strategy().context,
        InventorySkewedConfig(
            order_amount=D(1), volatility_samples=2, order_amount_shape_factor=D(0)
        ),
    )
    await warmup(selected)
    assert [
        row.quantity for row in selected.quotes(book(100), Account(D(1000), {"BTC": D(1)}))
    ] == [D(1), D(1)]
