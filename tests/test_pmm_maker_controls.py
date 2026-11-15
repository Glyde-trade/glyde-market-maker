"""PMM controls through real public decoding, risk admission and local paper orders."""

import asyncio
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal as D

import httpx
import pytest

from glyde_mm.client import OneApiClient, book_from_wire, trades_from_wire
from glyde_mm.config import Config
from glyde_mm.paper import PaperExchange
from glyde_mm.risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation
from glyde_mm.strategies.factory import create_strategy, validate_strategy_config
from glyde_mm.strategies.pure_market_making import StrategyContext
from glyde_mm.testing import FakeOneApiServer


@asynccontextmanager
async def stack(monkeypatch, tmp_path, *, max_position="10", **options):
    def forbidden(*args, **kwargs):
        raise AssertionError("paper maker controls must stay offline and credential-free")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(Config, "secret", forbidden)
    fake = FakeOneApiServer()
    fake.set_book("BTC", bids=[("99", "2")], asks=[("101", "2")])
    clock = [0.0]
    paper = PaperExchange(D(1000), clock_ms=lambda: fake.now_ms)
    paper.use_trade_tape()
    account = paper.account

    async def local_account():
        return replace(await account(), received_at=clock[0])

    monkeypatch.setattr(paper, "account", local_account)
    guard = RiskGuard(
        RiskLimits(max_position=D(max_position), max_order_notional=D(1000)),
        clock=lambda: clock[0],
        wall_clock=lambda: fake.now_ms / 1000,
        state_path=tmp_path / "risk.json",
    )
    managed = RiskManagedClient(paper, guard)
    strategy = create_strategy(
        "pure_market_making",
        StrategyContext(managed, "BTC", D("0.01"), D("0.001"), clock=lambda: clock[0]),
        {"order_amount": "0.1", "bid_spread": "0.002", "ask_spread": "0.002", **options},
    )
    config = Config(mode="paper", base_url="https://offline.invalid/v1")
    async with httpx.AsyncClient(transport=fake.http_transport()) as http:
        async with OneApiClient(config, http_client=http, connect_ws=fake.connect_ws) as api:
            assert "BTC" in {row["name"] for row in (await api.meta())["universe"]}
            stream = api.stream(
                [{"type": "l2Book", "coin": "BTC"}, {"type": "trades", "coin": "BTC"}]
            )

            async def feed(seconds=0, *, bid="99", ask="101"):
                clock[0] += seconds
                fake.advance_time(round(seconds * 1000))
                fake.set_book("BTC", bids=[(bid, "2")], asks=[(ask, "2")])
                while True:
                    frame = await asyncio.wait_for(anext(stream), 1)
                    if frame["channel"] == "l2Book" and frame["data"]["time"] == fake.now_ms:
                        break
                book = replace(book_from_wire(frame["data"]), received_at=clock[0])
                await paper.on_book(book)
                guard.update_book(book)
                await strategy.on_book(book)

            async def fill_buy():
                clock[0] += 0.1
                fake.advance_time(100)
                fake.market_order("BTC", buy=False, size="0.1")
                while True:
                    frame = await asyncio.wait_for(anext(stream), 1)
                    if frame["channel"] == "trades" and frame["data"]:
                        break
                for trade in trades_from_wire(frame["data"]):
                    await paper.on_trade(trade)
                assert len(paper.fills) == 1
                await strategy.on_fill(paper.fills[0])
                guard.update_account(await paper.account())

            try:
                await feed()
                yield paper, strategy, guard, feed, fill_buy
            finally:
                await stream.aclose()
                await managed.close()
    assert not fake._connections
    assert all(row["body"]["type"] == "meta" for row in fake.requests)
    assert fake.info({"type": "openOrders"}) == []


@pytest.mark.parametrize("multiplier,sizes", [("1", ("0.15", "0.05")), ("2", ("0.125", "0.075"))])
async def test_ratio_target_and_inventory_range_use_actual_equity(
    monkeypatch, tmp_path, multiplier, sizes
):
    async with stack(
        monkeypatch,
        tmp_path,
        inventory_skew_enabled=True,
        inventory_target_base_ratio="0.01",
        inventory_range_multiplier=multiplier,
    ) as (paper, strategy, guard, feed, fill):
        await strategy.on_tick()
        assert [(row.side, row.quantity) for row in await paper.open_orders()] == [
            ("buy", D(sizes[0])),
            ("sell", D(sizes[1])),
        ]
        assert len(guard.pending) == 2
        assert strategy.quotes(strategy.book, replace(await paper.account(), equity=D(0))) == ()


async def test_ratio_target_is_off_without_skew_flag_and_cannot_bypass_risk(monkeypatch, tmp_path):
    async with stack(monkeypatch, tmp_path, inventory_target_base_ratio="1") as (
        paper,
        strategy,
        *_,
    ):
        await strategy.on_tick()
        assert [row.quantity for row in await paper.open_orders()] == [D("0.1"), D("0.1")]
    async with stack(
        monkeypatch,
        tmp_path / "limited",
        max_position="0.1",
        inventory_skew_enabled=True,
        inventory_target_base_ratio="0.01",
    ) as (paper, strategy, *_):
        with pytest.raises(RiskViolation, match="max_position"):
            await strategy.on_tick()
        assert await paper.open_orders() == []


async def test_refresh_tolerance_keeps_real_ids_until_price_moves_outside(monkeypatch, tmp_path):
    async with stack(
        monkeypatch, tmp_path, order_refresh_time=1, order_refresh_tolerance_pct="0.1"
    ) as (paper, strategy, guard, feed, fill):
        await strategy.on_tick()
        original = [row.order_id for row in await paper.open_orders()]
        await feed(1, bid="99.05", ask="101.05")
        await strategy.on_tick()
        assert [row.order_id for row in await paper.open_orders()] == original
        await feed(1, bid="99.2", ask="101.2")
        await strategy.on_tick()
        assert set(original).isdisjoint(row.order_id for row in await paper.open_orders())
        assert len(guard.pending) == 2


async def test_filled_order_delay_cancels_remaining_quote_and_resumes_at_boundary(
    monkeypatch, tmp_path
):
    async with stack(monkeypatch, tmp_path, filled_order_delay=5) as (
        paper,
        strategy,
        guard,
        feed,
        fill,
    ):
        await strategy.on_tick()
        await fill()
        await strategy.on_tick()
        assert await paper.open_orders() == [] and guard.pending == {}
        await feed(4.999)
        await strategy.on_tick()
        assert await paper.open_orders() == []
        # A duplicate fill or unrelated identity cannot extend the deadline.
        await strategy.on_fill(paper.fills[0])
        await strategy.on_fill(replace(paper.fills[0], order_id="unrelated"))
        await feed(0.001)
        await strategy.on_tick()
        assert len(await paper.open_orders()) == 2
        assert len(paper.fills) == 1
        assert (await paper.account()).positions == {"BTC": D("0.1")}


async def test_fill_delay_is_disabled_by_default(monkeypatch, tmp_path):
    async with stack(monkeypatch, tmp_path) as (paper, strategy, guard, feed, fill):
        await strategy.on_tick()
        await fill()
        await strategy.on_tick()
        assert len(await paper.open_orders()) == 2 and len(guard.pending) == 2


async def test_levels_space_each_side_and_all_levels_share_position_cap(monkeypatch, tmp_path):
    async with stack(monkeypatch, tmp_path, order_levels=3, order_level_spread="0.01") as (
        paper,
        strategy,
        *_,
    ):
        await strategy.on_tick()
        assert [(row.side, row.price) for row in await paper.open_orders()] == [
            ("buy", D("99.8")),
            ("sell", D("100.2")),
            ("buy", D("98.8")),
            ("sell", D("101.2")),
            ("buy", D("97.8")),
            ("sell", D("102.2")),
        ]
    async with stack(monkeypatch, tmp_path / "limited", max_position="0.2", order_levels=3) as (
        paper,
        strategy,
        *_,
    ):
        with pytest.raises(RiskViolation, match="max_position"):
            await strategy.on_tick()
        assert await paper.open_orders() == []


@pytest.mark.parametrize(
    "values",
    [
        {"inventory_target_base_ratio": "1.01"},
        {"inventory_target_base_ratio": "-0.01"},
        {"inventory_target_base_ratio": "0.1", "inventory_target_position": "1"},
        {"filled_order_delay": True},
        {"filled_order_delay": -1},
        {"filled_order_delay": "nan"},
        {"order_refresh_tolerance_pct": "0.1", "refresh_tolerance_pct": "0.1"},
    ],
)
def test_invalid_or_ambiguous_controls_refuse(values):
    with pytest.raises(ValueError):
        validate_strategy_config("pure_market_making", values)


async def test_live_fill_delay_refuses_before_private_reads(monkeypatch, tmp_path):
    from glyde_mm.runner import run

    def forbidden(*args, **kwargs):
        raise AssertionError("unqualified live fill delay must refuse before credentials")

    monkeypatch.setattr(Config, "secret", forbidden)
    with pytest.raises(ValueError, match="identified paper-stream fills"):
        await run(
            Config(mode="live", strategy_config={"filled_order_delay": 5}),
            state_directory=tmp_path,
            report=lambda _: None,
        )
