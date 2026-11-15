"""Actual adapter + real SDK signatures + fake ledger; framework services are doubles."""

import asyncio
import json
import socket
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import httpx
import pytest
from eth_account import Account
from hummingbot_doubles import OrderType, PositionAction, TradeType, connector_modules
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

from glyde_mm.testing import FakeOneApiServer

OWNER = "0x" + "1" * 40
VAULT = "0x" + "2" * 40
READ_KEY = "offline-read-credential"
BASE = "http://127.0.0.1:8765/v1"


@pytest.fixture
def modules(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("connector tests cannot connect to the network")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)
    return connector_modules(monkeypatch)


class Gateway:
    """Modeled credential/signature boundary around an auth-free matching fixture."""

    def __init__(self, wallet):
        self.wallet = wallet
        self.fake = FakeOneApiServer(user=OWNER)
        self.fake.set_book("BTC", bids=[("99", "5")], asks=[("101", "5")])
        self.requests = []
        self.read_key = READ_KEY
        self.response_override = None

    def handle(self, request):
        body = json.loads(request.content)
        self.requests.append((request, body))
        if request.url.path.endswith("/info"):
            if "user" in body:
                if request.headers.get("X-Glyde-API-Key") != self.read_key or body["user"] != OWNER:
                    return httpx.Response(403)
            elif "X-Glyde-API-Key" in request.headers:
                raise AssertionError("public request leaked read credential")
            return httpx.Response(200, json=self.fake.info(body))
        if request.url.path.endswith("/exchange"):
            if body.get("vaultAddress") != VAULT:
                return httpx.Response(403)
            recovered = recover_agent_or_user_from_l1_action(
                body["action"], body["signature"], body["vaultAddress"], body["nonce"], None, True
            )
            if recovered.lower() != self.wallet.address.lower():
                return httpx.Response(403)
            if "X-Glyde-API-Key" in request.headers:
                raise AssertionError("read credential sent on exchange")
            result = self.fake.exchange(body, user=OWNER)
            return httpx.Response(200, json=self.response_override or result)
        raise AssertionError("unexpected endpoint")


@pytest.fixture
async def venue(modules):
    wallet = Account.create()
    gateway = Gateway(wallet)
    connector = modules.derivative.GlydePerpetualDerivative(
        OWNER, VAULT, wallet.key.hex(), READ_KEY, BASE, trading_pairs=["BTC-USD"]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway.handle)) as http:
        connector.http = http
        await connector._update_trading_rules()
        yield modules, gateway, connector


async def place(connector, *, cloid=None):
    return await connector._place_order(
        cloid or "0x" + "a" * 32,
        "BTC-USD",
        D("0.12345"),
        TradeType.BUY,
        OrderType.LIMIT_MAKER,
        D("99.1"),
        PositionAction.OPEN,
    )


async def test_real_auth_signed_order_decimal_owner_read_and_cancel(venue):
    _, gateway, connector = venue
    state = await connector._api_post("/info", data={"type": "clearinghouseState", "user": OWNER})
    assert state["marginSummary"]["accountValue"] == "10000"
    oid, timestamp = await place(connector)
    assert oid.isdigit() and timestamp == 123.5
    envelope = gateway.requests[-1][1]
    order = envelope["action"]["orders"][0]
    assert (order["p"], order["s"], order["t"], order["r"]) == (
        "99.1",
        "0.12345",
        {"limit": {"tif": "Alo"}},
        False,
    )
    assert envelope["vaultAddress"] == VAULT != connector.hyperliquid_perpetual_address
    assert json.loads(connector.authenticator.wire.last_envelope) == envelope
    assert await connector._place_cancel("0x" + "a" * 32, SimpleNamespace(trading_pair="BTC-USD"))
    cancelled = gateway.requests[-1][1]
    assert cancelled["action"]["type"] == "cancelByCloid"
    assert cancelled["nonce"] > envelope["nonce"]
    assert gateway.fake.info({"type": "openOrders", "user": OWNER}) == []


async def test_private_auth_never_accepts_vault_owner_or_missing_read_key(venue):
    _, gateway, connector = venue
    before = len(gateway.requests)
    with pytest.raises(ValueError, match="registered owner"):
        await connector._api_post("/info", data={"type": "clearinghouseState", "user": VAULT})
    assert len(gateway.requests) == before
    for user, headers in ((OWNER, {}), (VAULT, {"X-Glyde-API-Key": READ_KEY})):
        result = await connector.http.post(
            BASE + "/info", json={"type": "openOrders", "user": user}, headers=headers
        )
        assert result.status_code == 403
    gateway.read_key = "revoked"
    with pytest.raises(OSError, match="refused"):
        await connector._api_post("/info", data={"type": "openOrders", "user": OWNER})
    assert not connector.ready
    before = len(gateway.requests)
    with pytest.raises(OSError, match="suspended"):
        await place(connector)
    assert len(gateway.requests) == before


@pytest.mark.parametrize("mainnet", [True, False])
def test_signing_realm_and_vault_are_explicit_sdk_verified(modules, mainnet):
    wallet = Account.create()
    settings = modules.wire.Settings(OWNER, VAULT, wallet.key.hex(), READ_KEY, BASE, mainnet)
    wire = modules.wire.Wire(settings)
    signed = json.loads(wire.sign({"type": "cancel", "cancels": [{"a": 0, "o": 7}]}))

    def recover(vault, realm):
        return recover_agent_or_user_from_l1_action(
            signed["action"], signed["signature"], vault, signed["nonce"], None, realm
        ).lower()

    assert recover(VAULT, mainnet) == wallet.address.lower()
    assert recover(OWNER, mainnet) != wallet.address.lower()
    assert recover(VAULT, not mainnet) != wallet.address.lower()
    assert wallet.key.hex() not in repr(settings) and READ_KEY not in repr(settings)
    assert settings.ws_url == "ws://127.0.0.1:8765/v1/ws"
    with pytest.raises(ValueError, match="separate"):
        replace(settings, read_credential="")
    with pytest.raises(ValueError, match="explicit registered"):
        replace(settings, vault="")
    with pytest.raises(ValueError, match="domains are separate"):
        wire.sign({"type": "withdraw3"})


@pytest.mark.parametrize("price", ["100", "100.000", "100.123456789"])
async def test_integer_and_decimal_marks_do_not_change_size_or_asset_slots(venue, price):
    modules, _, connector = venue
    meta = {
        "universe": [
            {"name": "OLD", "szDecimals": 2, "isDelisted": True},
            {"name": "BTC", "szDecimals": 5},
        ]
    }
    result = await connector._format_trading_rules([meta, [{"markPx": price}, {"markPx": price}]])
    assert connector.coin_to_asset == {"BTC": 1}
    assert result[0].min_base_amount_increment == D("0.00001")
    assert result[0].min_price_increment == D("0.1")
    assert connector.quantize_order_price("BTC-USD", D("99.123")) == D("99.1")
    with pytest.raises(ValueError, match="decimal strings"):
        modules.wire.number(100.2)
    with pytest.raises(ValueError, match="size precision"):
        modules.wire.market_rules({"universe": [{"name": "BTC", "szDecimals": True}]})


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"status": "ok"},
        {"status": "ok", "response": {"type": "order", "data": {"statuses": []}}},
    ],
)
async def test_unknown_order_keeps_envelope_and_blocks_financial_actions(venue, result):
    _, gateway, connector = venue
    # Explicit empty override must still reach the malformed-response branch.
    original = gateway.handle

    def malformed(request):
        response = original(request)
        return (
            httpx.Response(200, json=result) if request.url.path.endswith("/exchange") else response
        )

    connector.http._transport = httpx.MockTransport(malformed)
    with pytest.raises(OSError, match="outcome unknown"):
        await place(connector)
    saved = connector.authenticator.wire.last_envelope
    before = len(gateway.requests)
    with pytest.raises(OSError, match="suspended"):
        await place(connector, cloid="0x" + "b" * 32)
    with pytest.raises(OSError, match="suspended"):
        await connector._api_post(
            "/exchange", data={"type": "updateLeverage", "asset": 0, "leverage": 1}
        )
    assert len(gateway.requests) == before
    assert connector.authenticator.wire.last_envelope == saved


async def test_auth_never_sends_credentials_to_foreign_endpoint(venue):
    _, gateway, connector = venue
    before = len(gateway.requests)
    with pytest.raises(ValueError, match="endpoint overrides"):
        await connector._api_request("/info", overwrite_url="https://other.invalid/info")
    with pytest.raises(ValueError, match="different endpoint"):
        await connector.authenticator.rest_authenticate(
            SimpleNamespace(url="https://other.invalid/info", data={}, headers={})
        )
    assert len(gateway.requests) == before


class WsAssistant:
    def __init__(self, gateway):
        self.gateway = gateway
        self.connection = None
        self.handshakes = []

    async def connect(self, *, ws_url, ws_headers=None, **kwargs):
        assert ws_url == BASE.replace("http:", "ws:") + "/ws"
        self.handshakes.append(ws_headers)
        if ws_headers != {"X-Glyde-API-Key": self.gateway.read_key}:
            raise OSError("read credential denied")
        self.connection = self.gateway.fake.connect_ws()
        await self.connection.__aenter__()

    async def send(self, request):
        sub = request.payload.get("subscription", {})
        if sub and sub.get("user") != OWNER:
            raise OSError("foreign owner denied")
        await self.connection.send(json.dumps(request.payload))

    async def disconnect(self):
        await self.connection.close()


async def test_real_fake_ws_owner_subscriptions_fill_delivery_dedup_and_disconnect(venue):
    modules, gateway, connector = venue
    ws = WsAssistant(gateway)

    async def get_ws_assistant():
        return ws

    factory = SimpleNamespace(get_ws_assistant=get_ws_assistant)
    source = modules.user_stream_data_source.GlydePerpetualUserStreamDataSource(
        connector=connector, api_factory=factory
    )
    queue = asyncio.Queue()
    await source._connected_websocket_assistant()
    try:
        await source._subscribe_channels(ws)
        assert ws.connection.subscriptions == [
            {"type": "orderUpdates", "user": OWNER},
            {"type": "userFills", "user": OWNER},
        ]
        for _ in range(3):  # Two actual subscription ACKs and the fill baseline.
            await source._process_event_message(json.loads(await ws.connection.recv()), queue)
        assert queue.get_nowait() == {"channel": "user", "data": {"fills": []}}
        await place(connector)
        gateway.fake.market_order("BTC", buy=False, size="0.12345")
        delivered = []
        for _ in range(3):  # Open update, filled update, actual fill.
            frame = json.loads(await ws.connection.recv())
            await source._process_event_message(frame, queue)
            if frame["channel"] == "userFills":
                delivered = frame
        rows = [queue.get_nowait() for _ in range(queue.qsize())]
        assert [row["channel"] for row in rows] == ["orderUpdates", "user", "orderUpdates"]
        actual_fill = next(row for row in rows if row["channel"] == "user")["data"]["fills"][0]
        assert D(actual_fill["px"]) == D("99.1") and D(actual_fill["sz"]) == D("0.12345")
        await source._process_event_message(delivered, queue)
        assert queue.get_nowait()["data"]["fills"] == []
        delivered["data"]["fills"][0]["px"] = "99.2"
        with pytest.raises(OSError, match="invalid"):
            await source._process_event_message(delivered, queue)
        assert not connector.ready
    finally:
        await source._on_user_stream_interruption(ws)
    assert not gateway.fake._connections
    # Reconnect must authenticate again; no automatic restoration of trading authority.
    await source._connected_websocket_assistant()
    assert ws.handshakes == [{"X-Glyde-API-Key": READ_KEY}] * 2
    assert not connector.ready
    await source._on_user_stream_interruption(ws)


@pytest.mark.parametrize(
    "frame",
    [
        {"channel": "error", "data": "expired"},
        {"channel": "resnapshot_required", "data": {}},
        {"channel": "userFills", "data": {"user": VAULT, "isSnapshot": False, "fills": []}},
    ],
)
async def test_private_stream_refusals_never_relax_owner_auth(venue, frame):
    modules, _, connector = venue
    source = modules.user_stream_data_source.GlydePerpetualUserStreamDataSource(
        connector=connector, api_factory=None
    )
    with pytest.raises(OSError, match="invalid"):
        await source._process_event_message(frame, asyncio.Queue())
    assert not connector.ready


async def test_missing_funding_context_remains_unavailable(venue):
    modules, _, connector = venue
    source = modules.api_order_book_data_source.GlydePerpetualAPIOrderBookDataSource(
        connector=connector, api_factory=None
    )
    with pytest.raises(OSError, match="refused"):
        await source.get_funding_info("BTC-USD")
    fields = modules.utils.GlydePerpetualConfigMap.model_fields
    assert {
        "glyde_perpetual_owner",
        "glyde_perpetual_vault_address",
        "glyde_perpetual_private_key",
        "glyde_perpetual_read_credential",
    } <= fields.keys()


async def test_lost_exchange_response_never_retries_or_regenerates_nonce(venue):
    _, gateway, connector = venue
    original = gateway.handle

    def lost(request):
        result = original(request)
        if request.url.path.endswith("/exchange"):
            raise httpx.ReadTimeout("synthetic lost response")
        return result

    connector.http._transport = httpx.MockTransport(lost)
    with pytest.raises(OSError, match="outcome unknown"):
        await place(connector)
    original_envelope = connector.authenticator.wire.last_envelope
    actions = [body for request, body in gateway.requests if request.url.path.endswith("/exchange")]
    assert len(actions) == 1
    assert len(gateway.fake.info({"type": "openOrders", "user": OWNER})) == 1
    with pytest.raises(OSError, match="suspended"):
        await place(connector)
    assert connector.authenticator.wire.last_envelope == original_envelope
    assert len(gateway.requests) == 2  # Original metadata and one exchange attempt.
    assert connector.unresolved_exchange_envelope == original_envelope
    connector.http._transport = httpx.MockTransport(gateway.handle)
    assert await connector._place_cancel("0x" + "a" * 32, SimpleNamespace(trading_pair="BTC-USD"))
    assert connector.unresolved_exchange_envelope == original_envelope
    assert gateway.fake.info({"type": "openOrders", "user": OWNER}) == []
    assert not connector.ready


def test_large_integer_prices_remain_exact_and_bad_public_urls_refuse(modules):
    assert modules.wire.quantize_price("123456", 5) == D("123456")
    wallet = Account.create()
    for base in (
        "http://outside.invalid/v1",
        "https://user:password@example.invalid/v1",
        "https://example.invalid/v1?key=x",
    ):
        with pytest.raises(ValueError, match="base URL"):
            modules.wire.Settings(OWNER, VAULT, wallet.key.hex(), READ_KEY, base)


@pytest.mark.parametrize(
    "receipt",
    [
        {"filled": {"oid": 1}},
        {"filled": {"oid": 1, "totalSz": "0.1"}},
        {"filled": {"oid": 1, "totalSz": "0", "avgPx": "99"}},
        {"filled": {"oid": 1, "totalSz": "-1", "avgPx": "99"}},
        {"filled": {"oid": 1, "totalSz": "NaN", "avgPx": "99"}},
        {"filled": {"oid": 1, "totalSz": "bad", "avgPx": "99"}},
        {"filled": {"oid": 1, "totalSz": "1", "avgPx": "99"}},
        {"filled": {"oid": 1, "totalSz": "0.1", "avgPx": "Infinity"}},
        {"filled": {"oid": 1, "totalSz": "0.1", "avgPx": "0"}},
        {"filled": {"oid": 1, "totalSz": "0.1", "avgPx": "-99"}},
        {"filled": {"oid": 1, "totalSz": "0.1", "avgPx": 99.1}},
        {"resting": {"oid": "01"}},
        {"resting": {"oid": "+1"}},
        {"resting": {"oid": " 1"}},
        {"resting": {"oid": "١"}},
        {"resting": {"oid": True}},
        {"resting": {"oid": 1.0}},
        {"resting": {"oid": 0}},
        {"resting": {"oid": -1}},
        {"resting": {"oid": 2**64}},
        {"resting": {"oid": "9" * 100}},
    ],
)
async def test_malformed_receipt_preserves_uncertainty(venue, receipt):
    _, gateway, connector = venue
    gateway.response_override = {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [receipt]}},
    }
    with pytest.raises(OSError, match="outcome unknown"):
        await place(connector)
    assert connector.unresolved_exchange_envelope == connector.authenticator.wire.last_envelope
    before = len(gateway.requests)
    with pytest.raises(OSError, match="suspended"):
        await place(connector)
    assert len(gateway.requests) == before and not connector.ready


async def test_complete_actual_filled_receipt_accepts_decimal_economics(venue):
    _, gateway, connector = venue
    oid, _ = await connector._place_order(
        "0x" + "f" * 32,
        "BTC-USD",
        D("0.12345"),
        TradeType.BUY,
        OrderType.MARKET,
        D(101),
        PositionAction.OPEN,
    )
    fills = gateway.fake.info({"type": "userFills", "user": OWNER})
    assert fills[0]["oid"] == int(oid)
    assert D(fills[0]["sz"]) == D("0.12345") and D(fills[0]["px"]) == D(101)
    assert connector.ready and connector.unresolved_exchange_envelope is None


def test_receipt_order_id_accepts_canonical_u64_boundaries(modules):
    assert modules.wire.canonical_order_id("1") == "1"
    assert modules.wire.canonical_order_id(2**64 - 1) == str(2**64 - 1)
