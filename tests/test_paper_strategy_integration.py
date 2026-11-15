"""Real foundation -> paper -> PMM -> risk composition, with no network or credentials."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from glyde_mm.client import OneApiClient, book_from_wire
from glyde_mm.config import Config
from glyde_mm.models import OrderRequest
from glyde_mm.paper import PaperExchange
from glyde_mm.strategies.pure_market_making import (
    PureMarketMakingConfig,
    PureMarketMakingStrategy,
    StrategyContext,
)

from glyde_mm.risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation
from glyde_mm.testing import FakeOneApiServer

D = Decimal


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("paper integration attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)
    monkeypatch.delenv("GLYDE_API_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GLYDE_READ_API_KEY", raising=False)


@asynccontextmanager
async def paper_stack(monkeypatch, tmp_path):
    fake = FakeOneApiServer()
    fake.set_book("BTC", bids=[("99", "2")], asks=[("101", "2")])
    config = Config(mode="paper", base_url="https://offline.invalid/v1", paper_balance=D("10000"))
    now = [0.0]
    paper = PaperExchange(config.paper_balance)
    actual_account = paper.account
    actual_place, actual_cancel, actual_cancel_all = (
        paper.place_orders,
        paper.cancel_orders,
        paper.cancel_all,
    )
    actions = []

    async def account_at_receipt():
        # Only the local receipt clock is injected; ledger values come from PaperExchange.
        return replace(await actual_account(), received_at=now[0])

    async def place(orders):
        actions.append(("place", tuple(await paper.open_orders())))
        return await actual_place(orders)

    async def cancel(orders):
        actions.append(("cancel", tuple(row.order_id for row in orders)))
        return await actual_cancel(orders)

    async def cancel_all(symbol=None):
        actions.append(("cancel_all", symbol))
        return await actual_cancel_all(symbol)

    monkeypatch.setattr(paper, "account", account_at_receipt)
    monkeypatch.setattr(paper, "place_orders", place)
    monkeypatch.setattr(paper, "cancel_orders", cancel)
    monkeypatch.setattr(paper, "cancel_all", cancel_all)
    guard = RiskGuard(
        RiskLimits(max_position=D("1"), max_order_notional=D("100"), max_daily_loss=D("1")),
        clock=lambda: now[0],
        wall_clock=lambda: fake.now_ms / 1000,
        state_path=tmp_path / "risk.json",
    )
    managed = RiskManagedClient(paper, guard)
    strategy = PureMarketMakingStrategy(
        StrategyContext(managed, "BTC", D("0.1"), D("0.00001"), clock=lambda: now[0]),
        PureMarketMakingConfig(
            bid_spread=D("0.002"),
            ask_spread=D("0.002"),
            order_amount=D("0.1"),
            order_refresh_time=30,
        ),
    )
    async with httpx.AsyncClient(transport=fake.http_transport()) as http:
        async with OneApiClient(config, http_client=http, connect_ws=fake.connect_ws) as api:
            # Exercise the foundation HTTP parser as well as its WebSocket decoder.
            assert (await api.book("BTC")).mid == D("100")
            stream = api.stream([{"type": "l2Book", "coin": "BTC"}])

            async def feed(seconds=0, *, bids=(("99", "2"),), asks=(("101", "2"),)):
                now[0] += seconds
                fake.advance_time(int(seconds * 1000))
                fake.set_book("BTC", bids=list(bids), asks=list(asks))
                while True:
                    frame = await asyncio.wait_for(anext(stream), timeout=1)
                    if frame["channel"] == "l2Book":
                        break
                book = replace(book_from_wire(frame["data"]), received_at=now[0])
                assert book.timestamp_ms == fake.now_ms
                await paper.on_book(book)
                guard.update_book(book)
                await strategy.on_book(book)
                return book

            try:
                await feed()
                yield fake, paper, managed, strategy, guard, feed, actions
            finally:
                try:
                    await stream.aclose()
                finally:
                    await managed.close()
    assert not fake._connections
    assert fake.requests and all(row["path"].endswith("/info") for row in fake.requests)
    assert fake.info({"type": "openOrders"}) == []


async def test_pmm_paper_quotes_refresh_cancel_before_replace_and_stop(monkeypatch, tmp_path):
    async with paper_stack(monkeypatch, tmp_path) as stack:
        fake, paper, managed, strategy, guard, feed, actions = stack
        await strategy.on_tick()
        initial = await paper.open_orders()
        assert [(row.side, row.price, row.quantity) for row in initial] == [
            ("buy", D("99.8"), D("0.1")),
            ("sell", D("100.2"), D("0.1")),
        ]
        initial_ids = {row.order_id for row in initial}
        assert len(initial_ids) == 2 and len(guard.pending) == 2
        assert all(row.order_id.startswith("paper-") for row in initial)
        assert fake.info({"type": "openOrders"}) == []

        await feed(29)
        await strategy.on_tick()
        assert {row.order_id for row in await paper.open_orders()} == initial_ids
        assert [action for action, _ in actions] == ["place"]

        await feed(1)
        await strategy.on_tick()
        refreshed = await paper.open_orders()
        assert len(refreshed) == 2
        assert initial_ids.isdisjoint(row.order_id for row in refreshed)
        assert {row.client_order_id for row in initial}.isdisjoint(
            row.client_order_id for row in refreshed
        )
        assert [action for action, _ in actions] == ["place", "cancel", "place"]
        assert set(actions[1][1]) == initial_ids
        assert actions[2][1] == ()  # The real ledger was empty before replacement placement.
        assert len(guard.pending) == 2

        await strategy.stop()
        assert strategy.blocked and await managed.open_orders() == []
        assert guard.pending == {}
        assert actions[-1] == ("cancel_all", "BTC")
        assert (await paper.account()).equity == D("10000")


async def test_actual_paper_mark_loss_trips_daily_kill_and_cannot_resume(monkeypatch, tmp_path):
    async with paper_stack(monkeypatch, tmp_path) as stack:
        fake, paper, managed, strategy, guard, feed, actions = stack
        # Seed inventory through the real risk boundary and finite paper-book depth.
        primer = OrderRequest("BTC", "buy", D("0.1"), D("101"), time_in_force="Ioc")
        assert (await managed.place_orders([primer]))[0].status == "filled"
        assert len(paper.fills) == 1
        assert paper.fills[0].price == D("101") and paper.fills[0].quantity == D("0.1")
        await strategy.on_tick()
        assert len(await paper.open_orders()) == 2
        assert (await paper.account()).positions == {"BTC": D("0.1")}

        # Widen only the bid: mark becomes 90 without filling either resting PMM quote.
        await feed(30, bids=(("79", "2"),), asks=(("101", "2"),))
        assert len(paper.fills) == 1
        assert (await paper.account()).equity == D("9998.9")
        with pytest.raises(RiskViolation, match="daily_loss"):
            await strategy.on_tick()
        assert strategy.blocked and guard.halted == "daily_loss"
        assert await paper.open_orders() == []
        assert guard.pending == {}
        assert [action for action, _ in actions] == ["place", "place", "cancel", "cancel_all"]
        state = json.loads((tmp_path / "risk.json").read_text())
        assert state["equity_start"] == "10000" and state["loss_halted"] is True
        placements = sum(action == "place" for action, _ in actions)

        await feed(1)
        await strategy.on_tick()
        with pytest.raises(RiskViolation, match="daily_loss"):
            await managed.place_orders([primer])
        assert guard.halted == "daily_loss" and await paper.open_orders() == []
        assert sum(action == "place" for action, _ in actions) == placements
        restarted = RiskGuard(guard.limits, state_path=tmp_path / "risk.json")
        assert restarted.halted == "daily_loss"
        assert all(row["path"].endswith("/info") for row in fake.requests)
