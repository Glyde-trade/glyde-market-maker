"""Changed-level refreshes, the quote action cap and rate/budget back-off (27 Nov 2026).

The live bot cancelled and re-placed every quote every 30s while Glyde's shared Hyperliquid
request budget was exhausted. These pin the cheaper refresh and the back-off.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal as D

import httpx
import pytest
from test_pure_market_making import Client, book
from test_risk import FakeClient, guard

from glyde_mm import client as client_module
from glyde_mm.client import EntryBucket, OneApiClient, RateLimited
from glyde_mm.config import Config
from glyde_mm.models import CancelRequest, OrderRequest, bot_client_order_id
from glyde_mm.risk import RiskManagedClient
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
    assert actions(client) == [("place", 4), ("cancel", 4), ("place", 4)]
    assert len(strategy._active) == 4


async def test_action_cap_defers_a_refresh_until_the_minute_budget_frees():
    client, now, strategy = setup(max_quote_actions_per_minute=6)
    await strategy.on_book(book("100"))
    await strategy.on_tick(0)
    assert actions(client) == [("place", 4)]

    now[0] = 31.0
    await strategy.on_book(book("102", timestamp=2))
    await strategy.on_tick(31)
    # Two entries left this minute: cancel two moved quotes, place nothing yet.
    assert actions(client) == [("place", 4), ("cancel", 2)]
    assert not strategy.blocked

    now[0] = 45.0
    await strategy.on_tick(45)  # Still throttled: no request at all.
    assert len(client.calls) == 2

    now[0] = 61.0
    await strategy.on_tick(61)
    # The t=31 cancels still count: cancel the last two, place the two inner levels.
    assert actions(client)[2:] == [("cancel", 2), ("place", 2)]
    assert [order.price for order in strategy._active.values()] == [D("100.98"), D("103.02")]

    now[0] = 92.0
    await strategy.on_tick(92)
    assert actions(client)[4:] == [("place", 2)] and len(strategy._active) == 4


async def test_rate_limit_refusal_backs_off_and_keeps_resting_quotes():
    client, now, strategy = setup()
    await strategy.on_book(book("100"))
    await strategy.on_tick(0)
    resting = dict(strategy._active)

    async def refused(requests):
        client.calls.append(("cancel", tuple(requests)))
        raise RateLimited("exchange refused the action: rate limited", 5.0)

    client.cancel_orders = refused
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


async def test_order_rate_limit_names_its_wait(unsigned):
    body = {
        "status": "err",
        "code": "ORDER_RATE_LIMITED",
        "response": "Glyde: ORDER_RATE_LIMITED; retry in 3s",
        "retryAfter": 3,
    }
    client = live_client(
        httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "3"}, json=body)
        )
    )
    with pytest.raises(RateLimited) as refused:
        await client.exchange({"type": "cancel", "cancels": []})
    assert refused.value.retry_after == 3.0
    assert str(refused.value) == "exchange refused the action: rate limited, retry in 3s"
    with pytest.raises(RateLimited) as in_body:
        OneApiClient._statuses(body, 1)
    assert in_body.value.retry_after == 3.0
    assert "retry in 3s" in str(in_body.value)


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


def cancel_receipts(sent, limits=None):
    """An exchange that records cancel batch sizes; /info answers glydeOrderRateLimits."""

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/info"):
            if limits is None:
                return httpx.Response(400, text="Glyde: unsupported request")
            return httpx.Response(200, json=limits)
        count = len(body["action"]["cancels"])
        sent.append(count)
        return httpx.Response(
            200,
            json={"status": "ok", "response": {"data": {"statuses": ["success"] * count}}},
        )

    return httpx.MockTransport(handler)


async def asset(symbol):
    return 0, None


@pytest.mark.paced
async def test_cancels_split_to_the_cancel_burst(unsigned, monkeypatch):
    # 28 Nov 2026 (#120): a 16-entry cancel used 16 of the account's 10-entry cancel burst.
    monkeypatch.setattr(client_module, "ORDER_ENTRY_RATES", {"cancel": (600, 10)})
    sent = []
    client = live_client(cancel_receipts(sent))
    monkeypatch.setattr(client, "_asset", asset)
    await client.cancel_orders([CancelRequest("BTC", order_id=str(i)) for i in range(25)])
    assert sent == [10, 10, 5]  # An older server reports no limits: the defaults apply.


@pytest.mark.paced
async def test_server_reported_limits_replace_the_defaults(unsigned, monkeypatch):
    sent = []
    limits = {
        "place": {"perMinute": 600, "burst": 30, "maxBatch": 40},
        "cancel": {"perMinute": 600, "burst": 4, "maxBatch": 6},
        "maxDebtSeconds": 10,
    }
    client = live_client(cancel_receipts(sent, limits))
    monkeypatch.setattr(client, "_asset", asset)
    await client.cancel_orders([CancelRequest("BTC", order_id=str(i)) for i in range(6)])
    assert sent == [4, 2]
    assert client._buckets["place"].burst == 30


@pytest.mark.paced
async def test_entry_bucket_paces_instead_of_asking_past_the_burst():
    bucket = EntryBucket(per_minute=600, burst=2)  # 10 entries a second.
    loop = asyncio.get_running_loop()
    started = loop.time()
    for _ in range(3):
        await bucket.take(2)
    assert loop.time() - started >= 0.35  # The burst, then two waits of 0.2s.
    bucket.refused(1.0)
    started = loop.time()
    await bucket.take(1)
    assert loop.time() - started >= 1.0


@pytest.mark.paced
async def test_client_waits_out_a_refusal_before_the_next_action(unsigned, monkeypatch):
    sent = []

    def handler(request):
        if request.url.path.endswith("/info"):
            return httpx.Response(400, text="Glyde: unsupported request")
        sent.append(asyncio.get_running_loop().time())
        if len(sent) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(
            200, json={"status": "ok", "response": {"data": {"statuses": ["success"]}}}
        )

    client = live_client(httpx.MockTransport(handler))
    with pytest.raises(RateLimited):
        await client.exchange({"type": "cancel", "cancels": [{"a": 0, "o": 1}]})
    await client.exchange({"type": "cancel", "cancels": [{"a": 0, "o": 1}]})
    assert sent[1] - sent[0] >= 1.0


async def test_rate_limited_sweep_cancel_is_resent_not_an_unknown_outcome(monkeypatch):
    # #120: a rate-refused cancel latched cancel_outcome_unknown and stopped the run.
    monkeypatch.setattr("glyde_mm.risk.asyncio.sleep", _no_sleep)
    g, _ = guard()
    raw = RateLimitedOnce()
    managed = RiskManagedClient(raw, g, cleanup_symbols=("BTC",))
    cloid = bot_client_order_id()
    g.pending[cloid] = OrderRequest("BTC", "buy", D("0.01"), D("99"), client_order_id=cloid)
    await managed.cancel_all("BTC")
    assert g.halted is None and raw.refusals == 2 and raw.all_cancels == 1


async def _no_sleep(seconds):
    return None


class RateLimitedOnce(FakeClient):
    def __init__(self):
        super().__init__()
        self.refusals = 0

    async def cancel_orders(self, orders):
        if self.refusals == 0:
            self.refusals += 1
            raise RateLimited("exchange refused the action: rate limited", 1.0)
        return await super().cancel_orders(orders)

    async def cancel_all(self, symbol=None):
        if self.refusals == 1:
            self.refusals += 1
            raise RateLimited("exchange refused the action: rate limited", 1.0)
        await super().cancel_all(symbol)
