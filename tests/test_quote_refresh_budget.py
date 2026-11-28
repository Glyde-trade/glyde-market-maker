"""Changed-level refreshes, the quote action cap and rate/budget back-off (27 Nov 2026).

The live bot cancelled and re-placed every quote every 30s while Glyde's shared Hyperliquid
request budget was exhausted. These pin the cheaper refresh and the back-off.
"""

from __future__ import annotations

from decimal import Decimal as D

import httpx
import pytest
from test_pure_market_making import Client, book

from glyde_mm.client import OneApiClient, RateLimited
from glyde_mm.config import Config
from glyde_mm.models import CancelRequest
from glyde_mm.strategies import PureMarketMakingConfig, PureMarketMakingStrategy, StrategyContext


def setup(**options):
    client, now = Client(), [0.0]
    context = StrategyContext(client, "BTC", D("0.01"), D("0.001"), clock=lambda: now[0])
    config = PureMarketMakingConfig(
        order_amount=D("1"),
        bid_spread=D("0.01"),
        ask_spread=D("0.01"),
        order_levels=2,
        order_level_spread=D("0.01"),
        quote_refresh="changed_levels",
        order_refresh_tolerance_pct=D("0.5"),
        **options,
    )
    return client, now, PureMarketMakingStrategy(context, config)


def actions(client):
    return [(kind, len(rows)) for kind, rows in client.calls]


async def test_changed_levels_keeps_quotes_within_tolerance_and_replaces_the_rest():
    client, now, strategy = setup()
    await strategy.on_book(book("100"))
    await strategy.on_tick(0)
    assert actions(client) == [("place", 4)]

    now[0] = 31.0
    await strategy.on_book(book("100.2", timestamp=2))  # 0.2% move: inside 0.5% tolerance.
    await strategy.on_tick(31)
    assert actions(client) == [("place", 4)]

    now[0] = 62.0
    await strategy.on_book(book("102", timestamp=3))  # 2% move: every level is out.
    await strategy.on_tick(62)
    # Each moved level keeps its order: one amend action, no cancel or placement.
    assert actions(client) == [("place", 4), ("modify", 4)]
    assert len(strategy._active) == 4


async def test_action_cap_defers_a_refresh_until_the_minute_budget_frees():
    client, now, strategy = setup(max_quote_actions_per_minute=6)
    await strategy.on_book(book("100"))
    await strategy.on_tick(0)
    assert actions(client) == [("place", 4)]

    now[0] = 31.0
    await strategy.on_book(book("102", timestamp=2))
    await strategy.on_tick(31)
    # Two entries left this minute: amend two moved quotes, nothing else yet.
    assert actions(client) == [("place", 4), ("modify", 2)]
    assert not strategy.blocked

    now[0] = 45.0
    await strategy.on_tick(45)  # Still throttled: no request at all.
    assert len(client.calls) == 2

    now[0] = 61.0
    await strategy.on_tick(61)
    # The t=31 amends still count: the last two moved quotes are amended now.
    assert actions(client)[2:] == [("modify", 2)]
    assert sorted(order.price for order in strategy._active.values()) == [
        D("99.96"),
        D("100.98"),
        D("103.02"),
        D("104.04"),
    ]
    assert len(strategy._active) == 4


async def test_rate_limit_refusal_backs_off_and_keeps_resting_quotes():
    client, now, strategy = setup()
    await strategy.on_book(book("100"))
    await strategy.on_tick(0)
    resting = dict(strategy._active)

    async def refused(requests):
        client.calls.append(("modify", tuple(requests)))
        raise RateLimited("exchange refused the action: rate limited", 5.0)

    client.modify_orders = refused
    now[0] = 31.0
    await strategy.on_book(book("102", timestamp=2))
    await strategy.on_tick(31)
    assert not strategy.blocked and strategy._active == resting
    calls = len(client.calls)

    now[0] = 45.0
    await strategy.on_tick(45)  # Inside the 30s back-off.
    assert len(client.calls) == calls

    now[0] = 62.0
    await strategy.on_tick(62)
    assert len(client.calls) == calls + 1  # Retried once, refused again: back-off doubles.
    assert strategy._throttled_until == pytest.approx(62.0 + 60.0)


def live_client(handler):
    config = Config(
        mode="live",
        base_url="https://offline.invalid/v1",
        owner="0x" + "1" * 40,
        vault_address="0x" + "2" * 40,
    )
    client = OneApiClient(config, http_client=httpx.AsyncClient(transport=handler))
    client._wallet = object()
    client._agent_wallet = lambda: None
    return client


@pytest.fixture
def unsigned(monkeypatch):
    monkeypatch.setattr("glyde_mm.client.sign_l1_action", lambda *args: {"r": "0x0"})


async def test_http_429_on_an_action_is_a_definite_rate_limit_refusal(unsigned):
    client = live_client(
        httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "7"}))
    )
    with pytest.raises(RateLimited) as refused:
        await client.exchange({"type": "cancel", "cancels": []})
    assert refused.value.retry_after == 7.0


def test_budget_refusal_text_is_a_rate_limit():
    with pytest.raises(RateLimited):
        OneApiClient._statuses({"status": "err", "response": "HL request budget exhausted"}, 1)


async def test_cancels_share_one_signed_action(unsigned, monkeypatch):
    sent = []

    def handler(request):
        sent.append(request)
        count = len(__import__("json").loads(request.content)["action"]["cancels"])
        return httpx.Response(
            200,
            json={"status": "ok", "response": {"data": {"statuses": ["success"] * count}}},
        )

    client = live_client(httpx.MockTransport(handler))

    async def asset(symbol):
        return 0, None

    monkeypatch.setattr(client, "_asset", asset)
    results = await client.cancel_orders([CancelRequest("BTC", order_id=str(i)) for i in range(10)])
    assert len(sent) == 1 and [r.status for r in results] == ["cancelled"] * 10
