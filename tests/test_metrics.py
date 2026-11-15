"""Offline metrics semantics and a loopback-only, read-only scrape lifecycle."""

import asyncio
import json
import time
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from live_wire import batch, owner_roots

from glyde_mm.client import OneApiError, account_from_wire
from glyde_mm.config import Config
from glyde_mm.live_state import LiveStateClient
from glyde_mm.metrics import Metrics
from glyde_mm.models import Book, CancelRequest, Level, OrderRequest
from glyde_mm.paper import PaperExchange
from glyde_mm.risk import RiskGuard, RiskLimits, RiskManagedClient, RiskViolation

OWNER = "0x" + "1" * 40


def book(mid=100, *, stamp=None, received=None):
    return Book(
        "BTC",
        (Level(D(mid) - 1, D(10)),),
        (Level(D(mid) + 1, D(10)),),
        stamp if stamp is not None else time.time_ns() // 1_000_000,
        received if received is not None else time.monotonic(),
    )


def value(metrics, name, labels=""):
    prefix = "glyde_mm_" + name + labels + " "
    return next(
        line[len(prefix) :]
        for line in metrics.render().decode().splitlines()
        if line.startswith(prefix)
    )


def test_default_does_not_bind_or_allocate_a_server(monkeypatch):
    monkeypatch.setattr(
        "glyde_mm.metrics.ThreadingHTTPServer", lambda *_: pytest.fail("disabled bind")
    )
    with Metrics(Config()) as metrics:
        assert metrics.server is None


@pytest.mark.parametrize(
    "settings",
    [
        {"metrics_enabled": "true"},
        {"metrics_port": True},
        {"metrics_port": -1},
        {"metrics_port": 65536},
    ],
)
def test_invalid_metrics_config_refuses(settings):
    with pytest.raises(ValueError):
        Config(**settings)


async def test_loopback_scrape_readonly_host_guard_no_labels_or_secret_values(capsys):
    config = Config(metrics_enabled=True, metrics_port=0, owner=OWNER, symbol=OWNER)
    with Metrics(config) as metrics:
        assert metrics.server.server_address[0] == "127.0.0.1"
        port = metrics.server.server_port
        metrics.refused("secret-url-with-private-key", 1)
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{port}/metrics")
            assert response.status_code == 200
            assert "version=0.0.4" in response.headers["Content-Type"]
            assert 'reason="other"' in response.text
            assert 'market="0"' in response.text
            assert OWNER not in response.text and "secret-url" not in response.text
            assert (
                await client.get(
                    f"http://127.0.0.1:{port}/metrics", headers={"Host": "evil.example"}
                )
            ).status_code == 403
            assert (
                await client.get(
                    f"http://127.0.0.1:{port}/metrics", headers={"Origin": "https://evil.example"}
                )
            ).status_code == 403
            assert (await client.post(f"http://127.0.0.1:{port}/metrics")).status_code == 501
            assert (await client.get(f"http://127.0.0.1:{port}/private")).status_code == 404
    assert not metrics._thread.is_alive()
    assert "secret" not in capsys.readouterr().err


async def test_real_risk_boundary_counts_definite_receipts_and_refusal_reasons():
    metrics = Metrics(Config(metrics_enabled=True))
    paper = PaperExchange(D(1000))
    guard = RiskGuard(RiskLimits(max_position=D(1), max_order_notional=D(25)))
    current = book()
    guard.update_book(current)
    await paper.on_book(current)
    managed = RiskManagedClient(paper, guard, metrics=metrics, cleanup_symbol="BTC")
    results = await managed.place_orders(
        [OrderRequest("BTC", "buy", D("0.1"), D(99)), OrderRequest("BTC", "sell", D("0.1"), D(102))]
    )
    assert value(metrics, "quotes_placed_total") == "2"
    request = CancelRequest("BTC", results[0].order_id)
    await managed.cancel_orders([request])
    await managed.cancel_orders([request])  # already_closed is not another cancellation
    assert value(metrics, "quotes_cancelled_total") == "1"
    await managed.place_orders([OrderRequest("BTC", "buy", D("0.1"), D(103))])
    assert value(metrics, "quotes_refused_total", '{reason="venue_rejected"}') == "1"
    with pytest.raises(RiskViolation, match="max_order_notional"):
        await managed.place_orders([OrderRequest("BTC", "buy", D(1), D(100))])
    assert value(metrics, "quotes_refused_total", '{reason="max_order_notional"}') == "1"
    assert value(metrics, "quotes_cancelled_total") == "2"
    assert await paper.open_orders() == []
    assert value(metrics, "quotes_placed_total") == "2"


async def test_exact_paper_pnl_and_headroom_without_cash_as_realized_pnl():
    metrics = Metrics(Config(metrics_enabled=True))
    paper = PaperExchange(D(1000))
    await paper.on_book(book())
    await paper.place_orders([OrderRequest("BTC", "buy", D("0.5"), D(101), time_in_force="Gtc")])
    metrics.snapshot(await paper.account(), D(1000), D(5), paper=paper, initial_balance=D(1000))
    assert value(metrics, "realized_pnl") == "0.0"
    assert value(metrics, "unrealized_pnl") == "-0.5"
    assert value(metrics, "daily_loss_headroom") == "4.5"
    await paper.on_book(book(104, stamp=time.time_ns() // 1_000_000 + 1))
    await paper.place_orders([OrderRequest("BTC", "sell", D("0.5"), D(103), time_in_force="Gtc")])
    metrics.snapshot(await paper.account(), D(1000), D(5), paper=paper, initial_balance=D(1000))
    assert value(metrics, "realized_pnl") == "1.0"
    assert value(metrics, "unrealized_pnl") == "0.0"
    assert value(metrics, "inventory", '{market="0"}') == "0.0"
    assert value(metrics, "daily_loss_headroom") == "6.0"


@pytest.mark.parametrize("pnl,expected", [("1.5", "1.5"), (None, "NaN"), ("not-a-number", "NaN")])
def test_live_pnl_uses_only_complete_supplied_facts(pnl, expected):
    state = owner_roots(OWNER, position="0.1")["clearinghouseState"]["clearinghouseState"]
    if pnl is not None:
        state["assetPositions"][0]["position"]["unrealizedPnl"] = pnl
    account = account_from_wire(state)
    metrics = Metrics(Config(metrics_enabled=True))
    metrics.snapshot(account, D(1000), D(5))
    assert value(metrics, "unrealized_pnl") == expected
    assert value(metrics, "realized_pnl") == "NaN"
    assert value(metrics, "inventory", '{market="0"}') == "0.1"


def test_staleness_advances_on_scrapes_without_repolling_or_restamping():
    now = [10.0]
    metrics = Metrics(
        Config(metrics_enabled=True), clock=lambda: now[0], wall_clock=lambda: 1000 + now[0]
    )
    metrics.book(book(stamp=1010000, received=10))
    metrics.account_cut(10, 1010)
    now[0] = 20
    for _ in range(3):
        assert float(value(metrics, "book_arrival_age_seconds", '{market="0"}')) == 10
        assert float(value(metrics, "book_source_age_seconds", '{market="0"}')) == 10
        assert float(value(metrics, "account_source_age_seconds")) == 10
    metrics.connected()
    assert value(metrics, "reconnects_total") == "0"
    metrics.connected()
    assert value(metrics, "reconnects_total") == "1"


def test_real_cache_gap_is_counted_once_and_does_not_restore_authority():
    metrics = Metrics(Config(metrics_enabled=True))
    cache = LiveStateClient(None, Config(owner=OWNER), metrics=metrics)
    for frame in batch("mm-owner", owner_roots(OWNER), owner=OWNER):
        cache.consume(frame)
    frames = batch("mm-owner", owner_roots(OWNER), owner=OWNER, sequence=3, previous=1)
    with pytest.raises(OneApiError):
        cache.consume(frames[0])
    assert value(metrics, "gaps_total") == "1"
    with pytest.raises(OneApiError):
        cache.consume(frames[0])
    assert value(metrics, "gaps_total") == "1"


async def test_enabled_runner_owns_endpoint_and_records_actual_paper_fills(monkeypatch, tmp_path):
    from glyde_mm import runner
    from glyde_mm.client import OneApiClient
    from glyde_mm.testing import FakeOneApiServer

    metrics = Metrics(Config(metrics_enabled=True, metrics_port=0))
    monkeypatch.setattr(runner, "Metrics", lambda _: metrics)
    fake = FakeOneApiServer(timestamp_ms=time.time_ns() // 1_000_000)
    fake.set_book("BTC", bids=[("99", "10")], asks=[("101", "10")])
    paper = PaperExchange(D(1000), clock_ms=lambda: fake.now_ms)
    placed, stop = asyncio.Event(), asyncio.Event()
    original = paper.place_orders

    async def place(rows):
        results = await original(rows)
        placed.set()
        return results

    monkeypatch.setattr(paper, "place_orders", place)
    http = httpx.AsyncClient(transport=fake.http_transport())
    source = OneApiClient(Config(), http_client=http, connect_ws=fake.connect_ws)

    async def feed():
        await placed.wait()
        fake.advance_time(1)
        fake.market_order("BTC", buy=False, size="0.001")
        for _ in range(20):
            await asyncio.sleep(0)
        assert value(metrics, "fills_total", '{market="0"}') == "1"
        stop.set()

    emitter = asyncio.create_task(feed())
    try:
        await asyncio.wait_for(
            runner.run(
                Config(
                    paper_balance=D(1000),
                    strategy_config={"bid_spread": "0.003", "ask_spread": "0.003"},
                ),
                state_directory=tmp_path,
                client_factory=lambda _: source,
                paper_factory=lambda _: paper,
                stop_event=stop,
                report=lambda _: None,
            ),
            3,
        )
        await emitter
        assert value(metrics, "quotes_placed_total") == "2"
        assert value(metrics, "quotes_cancelled_total") == "1"
        assert not metrics._thread.is_alive()
        assert await paper.open_orders() == []
        assert all(row["body"].get("type") == "meta" for row in fake.requests)
    finally:
        emitter.cancel()
        await asyncio.gather(emitter, return_exceptions=True)
        await http.aclose()


def test_grafana_sample_queries_only_fixed_metric_names():
    dashboard = json.loads(
        (Path(__file__).parents[1] / "docs/grafana-market-maker.json").read_text()
    )
    assert len(dashboard["panels"]) >= 6
    assert all(
        target["expr"].startswith("glyde_mm_")
        for panel in dashboard["panels"]
        for target in panel["targets"]
    )
    assert "0x" not in json.dumps(dashboard)


async def test_immediate_paper_fill_counter_is_idempotent_before_next_frame():
    metrics = Metrics(Config(metrics_enabled=True))
    paper = PaperExchange()
    await paper.on_book(book())
    await paper.place_orders([OrderRequest("BTC", "buy", D("0.01"), D(101), time_in_force="Gtc")])
    metrics.paper_fills(paper)
    metrics.paper_fills(paper)
    assert value(metrics, "fills_total", '{market="0"}') == "1"


async def test_actual_live_runner_ignores_duplicate_fill_history(monkeypatch, tmp_path):
    from live_fake import SequencedFake

    from glyde_mm import runner
    from glyde_mm.client import OneApiClient
    from glyde_mm.config import RiskConfig

    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", "0x" + "3" * 64)
    monkeypatch.setenv("GLYDE_READ_API_KEY", "read-fixture")
    metrics = Metrics(Config(metrics_enabled=True, metrics_port=0))
    monkeypatch.setattr(runner, "Metrics", lambda _: metrics)
    fake = SequencedFake()
    stop = asyncio.Event()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.http))
    config = Config(
        mode="live",
        owner=OWNER,
        vault_address="0x" + "4" * 40,
        risk=RiskConfig(max_position=D(1), max_order_notional=D(25)),
        strategy_config={"order_amount": "0.01", "order_refresh_time": 0.01},
    )
    source = OneApiClient(config, http_client=http, connect_ws=fake.connect)

    async def emit():
        await fake.refreshed.wait()
        fake.ledger.market_order("BTC", buy=False, size="0.005")
        fake.emit_owner()
        fake.emit_owner()  # historical fills repeated in the next valid owner cut
        for _ in range(24):
            await asyncio.sleep(0)
        assert value(metrics, "fills_total", '{market="0"}') == "1"
        stop.set()

    emitter = asyncio.create_task(emit())
    try:
        await asyncio.wait_for(
            runner.run(
                config,
                state_directory=tmp_path,
                client_factory=lambda _: source,
                stop_event=stop,
                report=lambda _: None,
            ),
            5,
        )
        await emitter
        assert fake.ledger.info({"type": "openOrders", "user": OWNER}) == []
        assert [r["type"] for r in fake.requests if "type" in r] == [
            "clearinghouseState",
            "openOrders",
            "meta",
        ]
    finally:
        emitter.cancel()
        await asyncio.gather(emitter, return_exceptions=True)
        await http.aclose()


async def test_occupied_port_fails_before_client_or_trading(tmp_path):
    from glyde_mm.runner import run

    with Metrics(Config(metrics_enabled=True, metrics_port=0)) as occupied:
        with pytest.raises(OSError):
            await run(
                Config(metrics_enabled=True, metrics_port=occupied.server.server_port),
                state_directory=tmp_path,
                client_factory=lambda _: pytest.fail("client opened"),
                report=lambda _: None,
            )
