from __future__ import annotations

import json
from contextlib import asynccontextmanager
from decimal import Decimal

import httpx
import pytest
from eth_account import Account as EthAccount
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

from glyde_mm.cli import main
from glyde_mm.client import OneApiClient, OneApiError, OutcomeUnknown, book_from_wire
from glyde_mm.config import Config, load_config, write_config
from glyde_mm.models import Book, CancelRequest, Level, ModifyRequest, OrderRequest
from glyde_mm.paper import PaperExchange

D = Decimal
# Ephemeral test-only wallet: no key material is committed or funded.
TEST_KEY = EthAccount.create().key.hex()
OWNER = "0x" + "22" * 20
CLOID = "0x" + "ab" * 16


class FakeOneApi:
    def __init__(self):
        self.requests = []
        self.actions = []
        self.orders = []
        self.fail_exchange = False
        self.cancel_fails = False

    def handle(self, request):
        body = json.loads(request.content)
        self.requests.append((request, body))
        if request.url.path == "/v1/exchange":
            self.actions.append(body)
            if self.fail_exchange:
                return httpx.Response(502, json={"secret": "must never appear in errors"})
            action = body["action"]
            if action["type"] in {"cancel", "cancelByCloid"}:
                if self.cancel_fails:
                    statuses = [{"error": "refused"}]
                else:
                    self.orders.clear()
                    statuses = ["success"]
            else:
                count = len(action.get("orders", action.get("modifies", [])))
                statuses = [{"resting": {"oid": 2**63 + i}} for i in range(count)]
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "response": {"type": action["type"], "data": {"statuses": statuses}},
                },
            )
        assert request.url.path == "/v1/info"
        responses = {
            "meta": {"universe": [{"name": "BTC", "szDecimals": 5}]},
            "allMids": {"BTC": "100.000000001"},
            "l2Book": {
                "coin": "BTC",
                "time": 1000,
                "levels": [[{"px": "99", "sz": "2"}], [{"px": "101", "sz": "2"}]],
            },
            "clearinghouseState": {
                "marginSummary": {"accountValue": "1000.00000001"},
                "assetPositions": [{"position": {"coin": "BTC", "szi": "-0.001"}}],
                "withdrawable": "900",
            },
            "openOrders": list(self.orders),
        }
        return httpx.Response(200, json=responses[body["type"]])


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("GLYDE_API_PRIVATE_KEY", TEST_KEY)
    monkeypatch.setenv("GLYDE_READ_API_KEY", "synthetic-read-token")
    server = FakeOneApi()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as http:
        async with OneApiClient(
            Config(mode="live", owner=OWNER, vault_address=OWNER), http_client=http
        ) as api:
            yield api, server


async def test_info_exact_decimals_and_private_header(client):
    api, server = client
    assert (await api.meta())["universe"][0]["name"] == "BTC"
    assert (await api.book("BTC")).mid == D(100)
    assert (await api.all_mids())["BTC"] == D("100.000000001")
    account = await api.account()
    assert account.equity == D("1000.00000001")
    assert account.positions == {"BTC": D("-0.001")}
    assert await api.open_orders() == []
    for request, body in server.requests:
        private = body["type"] in {"clearinghouseState", "openOrders"}
        assert ("x-glyde-api-key" in request.headers) == private


async def test_order_signature_uses_explicit_mainnet_and_exact_strings(client):
    api, server = client
    result = await api.place_orders(
        [OrderRequest("BTC", "buy", D("0.00001"), D("100.00000001"), CLOID)]
    )
    assert result[0].order_id == str(2**63)
    envelope = server.actions[0]
    wire = envelope["action"]["orders"][0]
    assert wire == {
        "a": 0,
        "b": True,
        "p": "100.00000001",
        "s": "0.00001",
        "r": False,
        "t": {"limit": {"tif": "Alo"}},
        "c": CLOID,
    }
    signer = recover_agent_or_user_from_l1_action(
        envelope["action"], envelope["signature"], OWNER, envelope["nonce"], None, True
    )
    assert signer == EthAccount.from_key(TEST_KEY).address
    wrong_domain = recover_agent_or_user_from_l1_action(
        envelope["action"], envelope["signature"], OWNER, envelope["nonce"], None, False
    )
    assert wrong_domain != signer


async def test_modify_cancel_and_cancel_by_cloid(client):
    api, server = client
    order = OrderRequest("BTC", "sell", D("0.01"), D(102))
    await api.modify_orders([ModifyRequest(str(2**63), order)])
    await api.cancel_orders([CancelRequest("BTC", order_id=str(2**63))])
    await api.cancel_orders([CancelRequest("BTC", client_order_id=CLOID)])
    assert [row["action"]["type"] for row in server.actions] == [
        "batchModify",
        "cancel",
        "cancelByCloid",
    ]
    assert server.actions[0]["action"]["modifies"][0]["oid"] == 2**63
    assert server.actions[2]["action"]["cancels"] == [{"asset": 0, "cloid": CLOID}]
    nonces = [row["nonce"] for row in server.actions]
    assert nonces == sorted(set(nonces))


async def test_unknown_send_has_no_automatic_retry_or_error_body_leak(client):
    api, server = client
    server.fail_exchange = True
    with pytest.raises(OutcomeUnknown) as caught:
        await api.place_orders([OrderRequest("BTC", "buy", D("0.01"), D(99))])
    assert len(server.actions) == 1
    assert "secret" not in str(caught.value)
    assert TEST_KEY not in str(caught.value)


async def test_cancel_all_requires_definitive_success(client):
    api, server = client
    server.orders = [{"coin": "BTC", "oid": 1, "side": "B", "sz": "0.01", "limitPx": "99"}]
    server.cancel_fails = True
    with pytest.raises(OneApiError):
        await api.cancel_all()
    assert len(server.orders) == 1
    server.cancel_fails = False
    await api.cancel_all()
    assert not server.orders
    assert server.requests[-1][1]["type"] == "openOrders"


async def test_paper_config_cannot_sign_or_post(client, monkeypatch):
    api, server = client
    api.config = Config()
    monkeypatch.delenv("GLYDE_API_PRIVATE_KEY")
    with pytest.raises(OneApiError, match="paper"):
        await api.place_orders([OrderRequest("BTC", "buy", D("0.01"), D(99))])
    assert server.actions == []


@pytest.mark.parametrize("quantity", [D("NaN"), D("Infinity"), D(-1), D(0), D("0.000001")])
async def test_invalid_order_never_reaches_exchange(client, quantity):
    api, server = client
    with pytest.raises(ValueError):
        await api.place_orders([OrderRequest("BTC", "buy", quantity, D(99))])
    assert not server.actions


async def test_websocket_injection_private_auth_and_bounded_options(monkeypatch):
    monkeypatch.setenv("GLYDE_READ_API_KEY", "synthetic-token")
    calls, sent = [], []

    class Socket:
        async def send(self, raw):
            sent.append(json.loads(raw))

        def __aiter__(self):
            return self.frames()

        async def frames(self):
            yield json.dumps({"channel": "orderUpdates", "data": []})

    @asynccontextmanager
    async def connect(url, **kwargs):
        calls.append((url, kwargs))
        yield Socket()

    async with OneApiClient(Config(owner=OWNER), connect_ws=connect) as api:
        frames = [frame async for frame in api.stream([{"type": "orderUpdates", "user": OWNER}])]
    assert frames == [{"channel": "orderUpdates", "data": []}]
    assert calls[0][0] == "wss://api.glyde.trade/v1/ws"
    assert calls[0][1]["additional_headers"] == {"X-Glyde-API-Key": "synthetic-token"}
    assert calls[0][1]["max_queue"] == 32
    assert sent[0]["method"] == "subscribe"


def book(bid="99", ask="101", quantity="1", time=1000):
    return Book("BTC", (Level(D(bid), D(quantity)),), (Level(D(ask), D(quantity)),), time)


async def test_paper_resting_fill_cancel_and_exact_perp_pnl():
    paper = PaperExchange(D(1000))
    await paper.on_book(book())
    result = await paper.place_orders([OrderRequest("BTC", "buy", D("0.5"), D(99), CLOID)])
    assert result[0].status == "resting"
    assert not paper.fills
    await paper.on_book(book("97", "98", time=1001))
    assert (await paper.account()).positions == {"BTC": D("0.5")}
    assert (await paper.account()).equity == D("999.75")
    assert await paper.open_orders() == []
    await paper.place_orders([OrderRequest("BTC", "sell", D("0.5"), D(100))])
    await paper.on_book(book("101", "102", time=1002))
    assert (await paper.account()).equity == D("1001.5")
    assert (await paper.account()).positions == {"BTC": D(0)}
    await paper.cancel_all()


async def test_paper_postonly_and_depth_not_reused_within_batch():
    paper = PaperExchange()
    await paper.on_book(book(quantity="0.5"))
    assert (await paper.place_orders([OrderRequest("BTC", "buy", D(1), D(101))]))[
        0
    ].status == "rejected"
    await paper.place_orders(
        [
            OrderRequest("BTC", "buy", D(1), D(101), time_in_force="Ioc"),
            OrderRequest("BTC", "buy", D(1), D(101), time_in_force="Ioc"),
        ]
    )
    assert sum(fill.quantity for fill in paper.fills) == D("0.5")
    with pytest.raises(ValueError, match="regressed"):
        await paper.on_book(book(time=999))


async def test_paper_reduce_only_does_not_create_or_flip_position():
    paper = PaperExchange()
    await paper.on_book(book())
    assert (
        await paper.place_orders(
            [OrderRequest("BTC", "sell", D(1), D(99), reduce_only=True, time_in_force="Ioc")]
        )
    )[0].status == "rejected"
    await paper.place_orders([OrderRequest("BTC", "buy", D("0.2"), D(101), time_in_force="Ioc")])
    await paper.place_orders(
        [OrderRequest("BTC", "sell", D(1), D(99), reduce_only=True, time_in_force="Ioc")]
    )
    assert (await paper.account()).positions["BTC"] == 0


def test_config_init_is_exclusive_private_paper_and_has_no_key(tmp_path):
    path = tmp_path / "config.yaml"
    write_config(path)
    selected = load_config(path)
    assert selected.mode == "paper"
    assert selected.risk.max_order_notional == D(10)
    assert path.stat().st_mode & 0o777 == 0o600
    assert TEST_KEY not in path.read_text()
    with pytest.raises(FileExistsError):
        write_config(path)
    assert main(["config", "init", "--output", str(tmp_path / "cli.yaml")]) == 0


def test_live_cli_blocked_until_risk_integration(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("mode: live\n")
    assert main(["run", "--config", str(path)]) == 1
    assert TEST_KEY not in capsys.readouterr().err


@pytest.mark.parametrize(
    "url",
    ["http://example.com", "https://user:password@example.com", "https://api.glyde.trade/?token=x"],
)
def test_config_refuses_insecure_or_credential_bearing_urls(url):
    with pytest.raises(ValueError):
        Config(base_url=url)


def test_book_rejects_crossed_and_preserves_decimals():
    raw = {
        "coin": "BTC",
        "time": 1000,
        "levels": [[{"px": "100.000000001", "sz": "0.1"}], [{"px": "101", "sz": "1"}]],
    }
    assert book_from_wire(raw).bids[0].price == D("100.000000001")
    raw["levels"][1][0]["px"] = "99"
    with pytest.raises(OneApiError):
        book_from_wire(raw)


async def test_websocket_application_heartbeat_is_joined_on_exit():
    import asyncio

    sent = []
    pinged = asyncio.Event()

    class Socket:
        async def send(self, raw):
            sent.append(json.loads(raw))
            if sent[-1].get("method") == "ping":
                pinged.set()

        def __aiter__(self):
            return self.frames()

        async def frames(self):
            async with asyncio.timeout(1):
                await pinged.wait()
            yield '{"channel":"pong"}'

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield Socket()

    async with OneApiClient(Config(), connect_ws=connect, heartbeat_interval=0.001) as api:
        assert [row async for row in api.stream([])] == [{"channel": "pong"}]
    count = len(sent)
    await asyncio.sleep(0.01)
    assert len(sent) == count


@pytest.mark.parametrize(
    "status",
    [
        {"resting": {"oid": None}},
        {"resting": {"oid": True}},
        {"resting": {"oid": "01"}},
        {"resting": {"oid": 0}},
        {"resting": {"oid": 2**64}},
        {"resting": {"oid": 1}, "error": "contradiction"},
        {"filled": {"oid": 1}},
        {"filled": {"oid": 1, "totalSz": "0", "avgPx": "100"}},
        {"filled": {"oid": 1, "totalSz": "2", "avgPx": "100"}},
        {"filled": {"oid": 1, "totalSz": "0.5", "avgPx": "101"}},
        {"filled": {"oid": 1, "totalSz": "NaN", "avgPx": "100"}},
        {"resting": {"oid": 1, "cloid": "wrong"}},
    ],
)
async def test_malformed_receipts_never_authorize_pending_release(client, status):
    api, _ = client
    result = {"status": "ok", "response": {"data": {"statuses": [status]}}}
    with pytest.raises(OutcomeUnknown):
        api._order_results(result, [OrderRequest("BTC", "buy", D(1), D(100))])


async def test_well_formed_filled_receipt_control(client):
    api, _ = client
    result = {
        "status": "ok",
        "response": {
            "data": {
                "statuses": [
                    {"filled": {"oid": 1, "totalSz": "0.5", "avgPx": "99"}},
                ]
            }
        },
    }
    assert (
        api._order_results(result, [OrderRequest("BTC", "buy", D(1), D(100))])[0].status == "filled"
    )


def test_decimal_wire_does_not_apply_context_rounding():
    from decimal import localcontext

    from glyde_mm.client import wire_decimal

    value = D("123456789012345678901234567890.12345678900")
    with localcontext() as context:
        context.prec = 6
        assert wire_decimal(value) == "123456789012345678901234567890.123456789"
        assert wire_decimal(D("1000")) == "1000"


async def test_paper_duplicate_cut_cannot_replay_consumed_liquidity():
    paper = PaperExchange()
    await paper.on_book(book())
    await paper.place_orders([OrderRequest("BTC", "buy", D(1), D(99))])
    crossing = book("97", "98", quantity="0.25", time=1001)
    await paper.on_book(crossing)
    assert sum(row.quantity for row in paper.fills) == D("0.25")
    await paper.on_book(crossing)
    assert sum(row.quantity for row in paper.fills) == D("0.25")
