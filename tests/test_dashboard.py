"""Local file/handler controls: no listeners, exchange requests or browser required."""

import io
import json
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from glyde_mm import dashboard
from glyde_mm.config import Config
from glyde_mm.models import Account, Book, Level, OrderRequest, Trade
from glyde_mm.paper import PaperExchange


def guard():
    return SimpleNamespace(
        account_state=Account(D(100)), pending={}, _equity_start=D(99), halted=None
    )


async def test_paper_snapshot_quotes_partial_fills_pnl_inventory_and_cleanup(tmp_path):
    config = Config(paper_balance=D(100))
    publisher = dashboard.SnapshotPublisher(tmp_path / "dashboard.json", config, "test-run")
    simulator = PaperExchange(D(100), clock_ms=lambda: 1000)
    simulator.use_trade_tape()
    await simulator.on_book(Book("BTC", (Level(D(99), D(1)),), (Level(D(101), D(1)),), 1000))
    await simulator.place_orders([OrderRequest("BTC", "buy", D(1), D(99))])
    await simulator.on_trade(Trade("BTC", "sell", D(".25"), D(98), 1001, "1"))
    await publisher.publish(guard(), paper=simulator)
    result = dashboard.read_snapshot(publisher.path)
    assert result["available"] and not result["stale"]
    row = result["snapshot"]
    assert row["inventory"] == {"BTC": "0.25"} and row["pnl"] == "0.25"
    assert row["quotes"][0]["quantity"] == "0.75"
    assert row["fills"][0]["quantity"] == "0.25" and row["fill_count"] == 1
    assert publisher.path.stat().st_mode & 0o777 == 0o600
    await simulator.cancel_all()
    await publisher.publish(guard(), paper=simulator)
    publisher.finish("stopped")
    final = dashboard.read_snapshot(publisher.path)
    assert final["stale"] and final["snapshot"]["status"] == "stopped"
    assert final["snapshot"]["quotes"] == []


async def test_live_snapshot_is_cached_equity_change_not_private_fill_authority(tmp_path):
    publisher = dashboard.SnapshotPublisher(
        tmp_path / "dashboard.json", Config(mode="live"), "live"
    )
    observed = guard()
    observed.pending["original"] = OrderRequest("BTC", "sell", D(1), D(101))
    observed.halted = "order_outcome_unknown"
    await publisher.publish(observed)
    row = dashboard.read_snapshot(publisher.path)["snapshot"]
    assert row["pnl"] == "1" and row["pnl_basis"] == "daily_equity_change"
    assert row["risk_stop"] == "order_outcome_unknown"
    assert row["fills"] is None and row["fill_count"] is None
    assert row["quotes_basis"] == "tracked_orders"


async def test_stale_missing_future_and_unknown_facts_are_not_live(tmp_path):
    path = tmp_path / "dashboard.json"
    assert dashboard.read_snapshot(path)["available"] is False
    publisher = dashboard.SnapshotPublisher(path, Config(), "starting")
    assert dashboard.read_snapshot(path)["snapshot"]["inventory"] is None
    await publisher.publish(guard())
    stamp = publisher.state["updated_at_ms"]
    assert not dashboard.read_snapshot(path, now_ms=stamp + 5000)["stale"]
    assert dashboard.read_snapshot(path, now_ms=stamp + 5001)["stale"]
    assert dashboard.read_snapshot(path, now_ms=stamp - 1)["stale"]


@pytest.mark.parametrize(
    "variant", ["symlink", "public", "oversize", "risk", "malformed", "directory"]
)
def test_refuses_unowned_shape_or_nonprivate_file(tmp_path, variant):
    path = tmp_path / "state"
    if variant == "directory":
        path.mkdir()
    else:
        path.write_text("{}")
        path.chmod(0o600)
        if variant == "symlink":
            link = tmp_path / "linked"
            link.symlink_to(path)
            path = link
        elif variant == "public":
            path.chmod(0o644)
        elif variant == "oversize":
            path.write_text(" " * 262145)
        elif variant == "risk":
            path.write_text('{"execution":{"originals":[]},"equity_start":"100"}')
        else:
            path.write_text("not json")
    assert dashboard.read_snapshot(path) == {"available": False, "stale": True, "snapshot": None}


async def test_unknown_fields_and_order_secrets_never_reach_response(tmp_path):
    publisher = dashboard.SnapshotPublisher(tmp_path / "state", Config(), "run")
    await publisher.publish(guard())
    row = publisher.state
    row["secret"] = "DO-NOT-SERVE"
    row["quotes"] = [
        {"symbol": "BTC", "side": "buy", "price": "99", "quantity": "1", "secret": "DO-NOT-SERVE"}
    ]
    publisher._write()
    assert "DO-NOT-SERVE" not in json.dumps(dashboard.read_snapshot(publisher.path))


def test_server_binds_loopback_only_and_has_no_mutation_routes(tmp_path, monkeypatch):
    captured = {}

    def server(address, handler):
        captured.update(address=address, handler=handler)
        return "server"

    monkeypatch.setattr(dashboard, "ThreadingHTTPServer", server)
    assert dashboard.make_server(tmp_path / "state", 8181) == "server"
    assert captured["address"] == ("127.0.0.1", 8181)
    assert "do_POST" not in captured["handler"].__dict__
    assert "do_DELETE" not in captured["handler"].__dict__
    assert b"textContent" in dashboard._PAGE and b"innerHTML" not in dashboard._PAGE


@pytest.mark.parametrize(
    "host,origin,path,status",
    [
        ("127.0.0.1:8181", None, "/state", 200),
        ("localhost:8181", "http://localhost:8181", "/", 200),
        ("evil.invalid:8181", None, "/state", 403),
        ("127.0.0.1:8181", "http://evil.invalid", "/state", 403),
        ("127.0.0.1:8181", None, "/.env", 404),
    ],
)
def test_readonly_handlers_and_rebinding_refusal(tmp_path, monkeypatch, host, origin, path, status):
    captured = {}
    monkeypatch.setattr(
        dashboard, "ThreadingHTTPServer", lambda address, handler: captured.update(handler=handler)
    )
    dashboard.make_server(tmp_path / "state", 8181)
    handler = object.__new__(captured["handler"])
    handler.server = SimpleNamespace(server_port=8181)
    handler.headers = {"Host": host, **({"Origin": origin} if origin else {})}
    handler.path, handler.wfile = path, io.BytesIO()
    responses, headers = [], {}
    handler.send_error = responses.append
    handler.send_response = responses.append
    handler.send_header = lambda key, value: headers.update({key: value})
    handler.end_headers = lambda: None
    handler.do_GET()
    assert responses == [status]
    if status == 200:
        assert headers["Cache-Control"] == "no-store"
        assert "Access-Control-Allow-Origin" not in headers


def test_dashboard_cli_reads_only_explicit_snapshot(tmp_path, monkeypatch):
    from glyde_mm.cli import main

    seen = []
    monkeypatch.setattr(dashboard, "serve", lambda path, port: seen.append((path, port)))
    monkeypatch.setattr(Config, "secret", lambda *_: pytest.fail("no credential access"))
    assert main(["dashboard", "--state", str(tmp_path / "state"), "--port", "8181"]) == 0
    assert seen == [(tmp_path / "state", 8181)]
