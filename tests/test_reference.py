from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from decimal import Decimal as D

import pytest

from glyde_mm.config import Config, RiskConfig
from glyde_mm.models import Account, Book, Level, OrderRequest
from glyde_mm.paper import PaperExchange
from glyde_mm.presets import strategy_preset
from glyde_mm.reference import HYPERLIQUID_PUBLIC_URL, ExternalReference
from glyde_mm.risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation
from glyde_mm.runner import run
from glyde_mm.strategies import PureMarketMakingConfig, PureMarketMakingStrategy, StrategyContext


def book(mid="100", *, timestamp=2000, received=2):
    mid = D(mid)
    return Book(
        "BTC", (Level(mid - D("0.01"), D(2)),), (Level(mid + D("0.01"), D(2)),), timestamp, received
    )


def reference(*, skew="0", limit="50", clock=lambda: 2):
    return ExternalReference(
        "BTC",
        max_deviation_bps=D(limit),
        skew_bps=D(skew),
        stale_seconds=D(3),
        clock=clock,
        wall_clock=clock,
    )


def test_exact_deviation_boundary_and_signed_fixed_skew():
    ref = reference(skew="5")
    ref.update(book())
    assert ref.center(book("100.5")) == D("100.05")
    with pytest.raises(RiskViolation, match="reference_deviation"):
        ref.center(book("100.5001"))
    ref.skew_bps = D(-5)
    assert ref.center(book()) == D("99.95")


def test_duplicate_source_does_not_reanchor_arrival_freshness():
    now = 2
    ref = reference(clock=lambda: now)
    ref.update(book())
    now = 6
    ref.update(book(received=6))
    with pytest.raises(RiskViolation, match="reference_stale"):
        ref.center(book())
    with pytest.raises(RiskViolation, match="reference_regressed"):
        ref.update(book(timestamp=1000, received=6))


def test_external_center_and_postonly_filter_use_actual_glyde_bbo():
    ref = reference()
    ref.update(book("100.2"))
    selected = PureMarketMakingStrategy(
        StrategyContext(None, "BTC", D("0.01"), D("0.001"), quote_center=ref.center),
        PureMarketMakingConfig(
            reference_source="hyperliquid", bid_spread=D("0.001"), ask_spread=D("0.001")
        ),
    )
    quotes = selected.quotes(book(), Account(D(1000)))
    assert [(row.side, row.price) for row in quotes] == [("sell", D("100.31"))]
    assert all(row.time_in_force == "Alo" for row in quotes)
    selected.context = replace(selected.context, quote_center=None)
    with pytest.raises(RiskViolation, match="reference_unavailable"):
        selected.quotes(book(), Account(D(1000)))


async def test_deviation_during_private_snapshot_await_is_rechecked_before_placement():
    ref = reference()
    ref.update(book())
    guard = RiskGuard(
        RiskLimits(), clock=lambda: 2, wall_clock=lambda: 2, reference_check=ref.center
    )
    guard.update_book(book())

    class DelayedClient:
        placed = False

        async def account(self):
            await asyncio.sleep(0)
            ref.update(book("102", timestamp=2001, received=2.001))
            return Account(D(1000), received_at=2)

        async def open_orders(self):
            return []

        async def place_orders(self, orders):
            self.placed = True
            pytest.fail("reference guard must run after private awaits")

        async def cancel_all(self, symbol=None):
            pass

    raw = DelayedClient()
    # Keep reference arrival and check on the same advancing monotonic cut.
    ref.clock = lambda: 2.001
    with pytest.raises(RiskViolation, match="reference_deviation"):
        await RiskManagedClient(raw, guard).place_orders(
            [OrderRequest("BTC", "buy", D("0.001"), D(99))]
        )
    assert not raw.placed and not guard.pending


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("healthy", None),
        ("deviate", "reference_deviation"),
        ("disconnect", "reference_disconnected"),
        ("stale", "reference_stale"),
        ("missing", "reference_disconnected"),
    ],
)
async def test_two_public_stream_runner_pulls_paper_quotes_and_never_leaks_authority(
    tmp_path, mode, reason, monkeypatch
):
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("fake stream runner must not connect")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    stop, placed = asyncio.Event(), asyncio.Event()
    selected = Config(
        owner="private-owner",
        vault_address="private-namespace",
        strategy_config={
            "order_amount": "0.01",
            "reference_source": "hyperliquid",
            "reference_stale_seconds": "0.1" if mode == "stale" else "3",
        },
        risk=RiskConfig(max_position=D("0.1"), max_order_notional=D(25)),
    )

    def frame(mid):
        now = time.time_ns() // 1_000_000
        return {
            "channel": "l2Book",
            "data": {
                "coin": "BTC",
                "time": now,
                "levels": [
                    [{"px": str(D(mid) - D("0.01")), "sz": "2"}],
                    [{"px": str(D(mid) + D("0.01")), "sz": "2"}],
                ],
            },
        }

    class Source:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def meta(self):
            return {"universe": [{"name": "BTC", "szDecimals": 2}]}

        async def stream(self, subscriptions):
            yield frame("100")
            while True:
                await asyncio.sleep(0.02)
                yield frame("100")

    class Ref:
        closed = False

        async def close(self):
            self.closed = True

        async def stream(self, subscriptions):
            assert subscriptions == [{"type": "l2Book", "coin": "BTC"}]
            if mode == "missing":
                return
            yield frame("100.2")
            await placed.wait()
            if mode == "deviate":
                yield frame("102")
            if mode == "disconnect":
                return
            await asyncio.Event().wait()

    class Paper(PaperExchange):
        submitted = []

        async def place_orders(self, orders):
            self.submitted.extend(orders)
            result = await super().place_orders(orders)
            placed.set()
            if mode == "healthy":
                stop.set()
            return result

    paper, external = Paper(selected.paper_balance), Ref()

    def ref_factory(public):
        assert public.base_url == HYPERLIQUID_PUBLIC_URL and public.mode == "paper"
        assert public.owner == "" and public.vault_address is None
        return external

    operation = run(
        selected,
        client_factory=lambda _: Source(),
        reference_factory=ref_factory,
        paper_factory=lambda _: paper,
        stop_event=stop,
        state_directory=tmp_path,
        report=lambda _: None,
    )
    if reason is None:
        await asyncio.wait_for(operation, 2)
    else:
        with pytest.raises(RiskViolation, match=reason):
            await asyncio.wait_for(operation, 2)
    assert external.closed and not await paper.open_orders()
    assert bool(paper.submitted) == (mode != "missing")
    assert all(order.side == "sell" for order in paper.submitted)  # crossing buys omitted


@pytest.mark.parametrize(
    "preset,limit", [("conservative", "25"), ("balanced", "50"), ("active", "100")]
)
def test_external_beginner_presets_are_bounded_and_decimal(preset, limit):
    values = strategy_preset(
        "pure_market_making", preset, D(100000), D(25), D("0.001"), reference_source="hyperliquid"
    )
    assert values["max_reference_deviation_bps"] == limit
    assert values["reference_skew_bps"] == "0"
    with pytest.raises(ValueError):
        strategy_preset(
            "grid", preset, D(100000), D(25), D("0.001"), reference_source="hyperliquid"
        )


@pytest.mark.parametrize(
    "values",
    [
        {"reference_source": "other"},
        {"reference_skew_bps": D(1001)},
        {"max_reference_deviation_bps": D(0)},
        {"reference_stale_seconds": D("NaN")},
    ],
)
def test_invalid_reference_settings_refuse(values):
    with pytest.raises(ValueError):
        PureMarketMakingConfig(**values)


async def test_real_public_transport_has_no_credentials_or_private_frames(monkeypatch):
    import json
    from contextlib import asynccontextmanager

    from glyde_mm.client import OneApiClient

    sent = []

    class Socket:
        async def send(self, raw):
            sent.append(json.loads(raw))

        def __aiter__(self):
            return self.frames()

        async def frames(self):
            yield '{"channel":"subscriptionResponse","data":{}}'

    @asynccontextmanager
    async def connect(url, **kwargs):
        assert url == "wss://api.hyperliquid.xyz/ws"
        assert kwargs["additional_headers"] == {}
        yield Socket()

    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "DO-NOT-SEND")
    monkeypatch.setenv("GLYDE_READ_API_KEY", "DO-NOT-SEND")
    async with OneApiClient(Config(base_url=HYPERLIQUID_PUBLIC_URL), connect_ws=connect) as client:
        frames = [row async for row in client.stream([{"type": "l2Book", "coin": "BTC"}])]
    assert len(frames) == 1
    assert sent == [{"method": "subscribe", "subscription": {"type": "l2Book", "coin": "BTC"}}]


async def test_single_source_replay_never_falls_back_to_glyde_mid(tmp_path):
    import json

    from glyde_mm.replay import replay

    path = tmp_path / "books.jsonl"
    header = {"format": "glyde.public-book", "version": 1, "symbol": "BTC", "size_decimals": 2}
    rows = [header]
    for second in (0, 60):
        stamp = 1700000000000 + second * 1000
        rows.append(
            {
                "received_at_ms": stamp,
                "book": {
                    "coin": "BTC",
                    "time": stamp,
                    "levels": [[{"px": "99", "sz": "2"}], [{"px": "101", "sz": "2"}]],
                },
            }
        )
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = await replay(Config(strategy_config={"reference_source": "hyperliquid"}), path, 1)
    assert result.risk_stop == "reference_unavailable"
    assert result.fills == 0 and result.pnl == 0
