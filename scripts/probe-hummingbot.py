"""Offline stock-method wire probe; not a full Hummingbot runtime qualification."""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import logging
import socket
from collections import OrderedDict
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import eth_account
import msgpack
from eth_account.messages import encode_typed_data
from eth_utils import is_hex_address, keccak, to_checksum_address, to_hex
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

from glyde_mm.testing import FakeOneApiServer

OWNER = "0x" + "1" * 40
VAULT = "0x" + "2" * 40
BASE = "http://offline.invalid/v1"


class OrderType(Enum):
    LIMIT = 1
    LIMIT_MAKER = 2
    MARKET = 3


class PositionAction(Enum):
    NIL = 1
    CLOSE = 2


class TradeType(Enum):
    BUY = 1
    SELL = 2


class PositionSide(Enum):
    LONG = 1
    SHORT = 2


def load_stock(path, namespace, *, class_name=None, methods=()):
    tree = ast.parse(path.read_text())
    if class_name:
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
        node.bases = []
        if methods:
            node.body = [
                n
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in methods
            ]
            if {n.name for n in node.body} != set(methods):
                raise ValueError("upstream method set changed")
        nodes = [node]
    else:
        nodes = [
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in methods
        ]
        if {n.name for n in nodes} != set(methods):
            raise ValueError("upstream function set changed")
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            *nodes,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace.get(class_name)


async def probe(root):
    def deny_network(*args, **kwargs):
        raise AssertionError("this probe must never connect to a real endpoint")

    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    constants = {}
    for node in ast.parse((root / "hyperliquid_perpetual_constants.py").read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                constants[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                if isinstance(node.value, ast.Name) and node.value.id in constants:
                    constants[node.targets[0].id] = constants[node.value.id]
    constants.update(PERPETUAL_BASE_URL=BASE, PERPETUAL_WS_URL="ws://offline.invalid/v1/ws")
    namespace = dict(
        CONSTANTS=SimpleNamespace(**constants),
        Decimal=Decimal,
        ROUND_HALF_UP=ROUND_HALF_UP,
        json=json,
        time=__import__("time"),
        asyncio=asyncio,
        OrderedDict=OrderedDict,
        eth_account=eth_account,
        msgpack=msgpack,
        encode_typed_data=encode_typed_data,
        is_hex_address=is_hex_address,
        keccak=keccak,
        to_checksum_address=to_checksum_address,
        to_hex=to_hex,
        PositionAction=PositionAction,
        OrderType=OrderType,
        TradeType=TradeType,
        PositionSide=PositionSide,
        TradingRule=lambda pair, **kw: SimpleNamespace(trading_pair=pair, **kw),
        Position=lambda **kw: SimpleNamespace(**kw),
        WSJSONRequest=lambda **kw: SimpleNamespace(**kw),
    )
    load_stock(
        root / "hyperliquid_perpetual_web_utils.py",
        namespace,
        methods=(
            "float_to_wire",
            "order_type_to_wire",
            "order_spec_to_order_wire",
            "rest_url",
            "wss_url",
        ),
    )
    auth_type = load_stock(
        root / "hyperliquid_perpetual_auth.py", namespace, class_name="HyperliquidPerpetualAuth"
    )
    methods = (
        "_make_trading_rules_request",
        "_format_trading_rules",
        "_iter_hip3_merged_markets",
        "_place_order",
        "_place_cancel",
        "_process_cancel_result",
        "_update_balances",
        "_update_positions",
        "_verify_key_authority",
        "_uses_spot_balances",
        "_get_user_abstraction_mode",
        "_is_testnet",
        "_should_inject_builder",
        "_build_builder_field",
        "_ensure_builder_fee_resolved",
        "_initialize_builder_fee",
    )
    connector_type = load_stock(
        root / "hyperliquid_perpetual_derivative.py",
        namespace,
        class_name="HyperliquidPerpetualDerivative",
        methods=methods,
    )
    book_type = load_stock(
        root / "hyperliquid_perpetual_api_order_book_data_source.py",
        namespace,
        class_name="HyperliquidPerpetualAPIOrderBookDataSource",
        methods=("_request_order_book_snapshot",),
    )
    stream_type = load_stock(
        root / "hyperliquid_perpetual_user_stream_data_source.py",
        namespace,
        class_name="HyperliquidPerpetualUserStreamDataSource",
        methods=("_subscribe_channels",),
    )
    fake = FakeOneApiServer(user=OWNER)
    fake.set_book("BTC", bids=[("99", "2")], asks=[("101", "2")])
    wallet = eth_account.Account.create()  # Ephemeral fixture key; never retained or printed.
    auth = auth_type(VAULT, wallet.key.hex(), True)
    connector = connector_type()
    connector.hyperliquid_perpetual_address = VAULT
    connector.hyperliquid_perpetual_secret_key = wallet.key.hex()
    connector._use_vault = True
    connector._domain = constants["DOMAIN"]
    connector._enable_hip3_markets = False
    connector._dex_markets = []
    connector._builder_fee_resolved = True
    connector._key_authority_verified = False
    connector._user_abstraction_mode = None
    connector._account_balances = {}
    connector._account_available_balances = {}
    connector.coin_to_asset = {"BTC": 0}
    connector.trading_rules_request_path = "/info"
    connector.current_timestamp = 0
    logger = logging.getLogger("hummingbot-probe")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    connector.logger = lambda: logger
    positions = {}
    connector._perpetual_trading = SimpleNamespace(
        account_positions=positions,
        position_key=lambda pair, side: (pair, side),
        set_position=lambda key, value: positions.__setitem__(key, value),
        remove_position=lambda key: positions.pop(key, None),
    )

    async def symbol(*args, **kwargs):
        return "BTC"

    async def pair(*args, **kwargs):
        return "BTC-USD"

    connector.exchange_symbol_associated_to_pair = symbol
    connector.trading_pair_associated_to_exchange_symbol = pair
    private_control = False
    wire_types = []

    async def post(path_url, data, is_auth_required=False):
        wire_types.append(data["type"])
        if path_url == "/exchange":
            signed = json.loads(auth.add_auth_to_params_post(json.dumps(data), BASE + path_url))
            assert signed["vaultAddress"] == VAULT
            recovered = recover_agent_or_user_from_l1_action(
                signed["action"], signed["signature"], VAULT, signed["nonce"], None, True
            )
            assert recovered.lower() == wallet.address.lower()
            return fake.exchange(signed)
        if data["type"] == "metaAndAssetCtxs":
            # Public contexts are a fixture extension, not a probed backend provider.
            return [fake.info({"type": "meta"}), [{"markPx": "100.0"}, {"markPx": "100.0"}]]
        if data["type"] in {"clearinghouseState", "openOrders"}:
            if not private_control:
                raise PermissionError("missing X-Glyde-API-Key read credential")
            if data["user"] != OWNER:
                raise PermissionError(
                    "private user must match credential owner, not vault namespace"
                )
        return fake.info(data)

    connector._api_post = post
    results = {
        "scope": "unmodified extracted stock methods; framework/HTTP scheduler not instantiated"
    }
    assert namespace["rest_url"]("/info") == BASE + "/info"
    assert namespace["wss_url"]() == "ws://offline.invalid/v1/ws"
    info = await connector._make_trading_rules_request()
    rules = await connector._format_trading_rules(info)
    assert len(rules) == 2 and rules[0].min_base_amount_increment == Decimal("0.00001")
    results["exchange_info"] = (
        "PASS stock metaAndAssetCtxs request/parser against synthetic contexts (fake extension)"
    )
    integer_info = [info[0], [{"markPx": "100"}, {"markPx": "100"}]]
    assert await connector._format_trading_rules(integer_info) == []
    results["integer_mark_control"] = (
        "INCOMPATIBLE: stock parser splits markPx at '.', silently skips integer-form marks"
    )
    book = book_type()
    book._connector = connector
    snapshot = await book._request_order_book_snapshot("BTC-USD")
    assert snapshot["levels"][0][0]["px"] == "99"
    results["order_book"] = "PASS stock l2Book request against fake"
    oid = "0x" + "a" * 32
    exchange_id, _ = await connector._place_order(
        oid, "BTC-USD", Decimal("0.1"), TradeType.BUY, OrderType.LIMIT_MAKER, Decimal("98")
    )
    assert str(fake.info({"type": "openOrders", "user": OWNER})[0]["oid"]) == exchange_id
    results["place"] = (
        "PASS isolated stock Alo order+vault signature, SDK mainnet recovery "
        "and fake resting receipt; bootstrap bypassed"
    )
    assert await connector._place_cancel(oid, SimpleNamespace(trading_pair="BTC-USD"))
    assert fake.info({"type": "openOrders", "user": OWNER}) == []
    results["cancel"] = "PASS stock cancelByCloid+signature+fake success receipt"
    for label, method in (
        ("balances", connector._update_balances),
        ("positions", connector._update_positions),
    ):
        try:
            await method()
        except PermissionError as error:
            results[label] = f"INCOMPATIBLE: {error}"
        else:
            raise AssertionError("unadapted stock private read unexpectedly passed")
    private_control = True
    try:
        await connector._update_positions()
    except PermissionError as error:
        results["owner_vault_binding"] = f"INCOMPATIBLE: {error}"
    else:
        raise AssertionError("vault namespace unexpectedly accepted as credential owner")
    connector.hyperliquid_perpetual_address = (
        OWNER  # Deliberate owner/vault adaptation for shape-only control.
    )
    await connector._update_balances()
    fake.market_order("BTC", buy=True, size="0.1", user=OWNER)
    await connector._update_positions()
    assert connector._account_balances["USD"] == Decimal("10000")
    assert next(iter(positions.values())).amount == Decimal("0.1")
    results["private_shape_control"] = (
        "PASS balances/positions only after modeled read-header permission "
        "and separate owner/vault adaptation"
    )
    stream = stream_type()
    stream._connector = connector
    stream.logger = lambda: logger
    payloads = []

    class Assistant:
        def __init__(self, ws, *, authenticated):
            self.ws, self.authenticated = ws, authenticated

        async def send(self, request):
            payloads.append(request.payload)
            assert await auth.ws_authenticate(request) is request
            if not self.authenticated:
                raise PermissionError("private subscription requires Glyde read authentication")
            await self.ws.send(json.dumps(request.payload))

    async with fake.connect_ws() as ws:
        try:
            await stream._subscribe_channels(Assistant(ws, authenticated=False))
        except PermissionError as error:
            results["user_stream_auth"] = f"INCOMPATIBLE (modeled boundary): {error}"
        else:
            raise AssertionError("unauthenticated stock stream unexpectedly passed")
        payloads.clear()
        try:
            await stream._subscribe_channels(Assistant(ws, authenticated=True))
        except ValueError as error:
            assert str(error) == "unsupported subscription"
            assert [row["subscription"]["type"] for row in payloads] == ["orderUpdates", "user"]
            results["user_stream_channel"] = "INCOMPATIBLE: fake rejects stock user subscription"
        else:
            raise AssertionError("stock user channel unexpectedly passed")
        await ws.send(
            json.dumps(
                {"method": "subscribe", "subscription": {"type": "userFills", "user": OWNER}}
            )
        )
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), 1))
            if frame["channel"] == "userFills":
                break
        assert len(frame["data"]["fills"]) == 1
    assert not fake._connections
    results["user_stream_control"] = "PASS adapted authenticated userFills snapshot against fake"
    results["requests"] = wire_types
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source_directory", type=Path, help="local upstream hyperliquid_perpetual directory"
    )
    args = parser.parse_args()
    asyncio.run(probe(args.source_directory))
