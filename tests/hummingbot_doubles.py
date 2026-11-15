"""Minimal Hummingbot service doubles; never a complete framework startup test."""

import asyncio
import importlib.util
import json
import sys
import types
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel


class OrderType(Enum):
    LIMIT = 1
    LIMIT_MAKER = 2
    MARKET = 3


class TradeType(Enum):
    BUY = 1
    SELL = 2


class PositionAction(Enum):
    NIL = 1
    OPEN = 2
    CLOSE = 3


class SymbolMap(dict):
    @property
    def inverse(self):
        return {v: k for k, v in self.items()}


class FrameworkDerivative:
    def __init__(self, **kwargs):
        self.hyperliquid_perpetual_address = kwargs["hyperliquid_perpetual_address"]
        self._domain = kwargs["domain"]
        self._trading_pairs = kwargs["trading_pairs"]
        self._auth = self.authenticator
        self.current_timestamp = 123.5

    @property
    def ready(self):
        return True

    async def _api_post(self, path_url, **kwargs):
        return await self._api_request(path_url=path_url, **kwargs)

    async def _api_request(self, path_url, data=None, is_auth_required=False, **kwargs):
        request = SimpleNamespace(
            url=await self._api_request_url(path_url), data=data, headers=None
        )
        if is_auth_required:
            request = await self._auth.rest_authenticate(request)
        payload = request.data if isinstance(request.data, str) else json.dumps(request.data)
        result = await self.http.post(request.url, content=payload, headers=request.headers)
        result.raise_for_status()
        return result.json()

    def _set_trading_pair_symbol_map(self, mapping):
        self._trading_pair_symbol_map = mapping

    async def trading_pair_associated_to_exchange_symbol(self, symbol):
        return self._trading_pair_symbol_map[symbol]

    async def exchange_symbol_associated_to_pair(self, trading_pair):
        return self._trading_pair_symbol_map.inverse[trading_pair]


class FrameworkStream:
    def __init__(self, connector, api_factory, **kwargs):
        self._connector = connector
        self._api_factory = api_factory
        self._ping_task = None

    async def _get_ws_assistant(self):
        return await self._api_factory.get_ws_assistant()

    async def _on_user_stream_interruption(self, ws):
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        if ws:
            await ws.disconnect()

    def _next_funding_time(self):
        return 3600


def connector_modules(monkeypatch):
    def module(name, **members):
        if name in sys.modules:
            return sys.modules[name]
        if "." in name:
            module(name.rsplit(".", 1)[0])
        result = types.ModuleType(name)
        result.__path__ = []
        result.__dict__.update(members)
        monkeypatch.setitem(sys.modules, name, result)
        return result

    root = "hummingbot.connector.derivative.hyperliquid_perpetual."
    module(
        root + "hyperliquid_perpetual_derivative",
        HyperliquidPerpetualDerivative=FrameworkDerivative,
    )
    module(
        root + "hyperliquid_perpetual_user_stream_data_source",
        HyperliquidPerpetualUserStreamDataSource=FrameworkStream,
    )
    module(
        root + "hyperliquid_perpetual_api_order_book_data_source",
        HyperliquidPerpetualAPIOrderBookDataSource=FrameworkStream,
    )
    module("hummingbot.core.web_assistant.auth", AuthBase=object)
    module("hummingbot.core.utils.async_utils", safe_ensure_future=asyncio.ensure_future)
    module("hummingbot.core.web_assistant.connections.data_types", WSJSONRequest=SimpleNamespace)
    module(
        "hummingbot.core.data_type.common",
        OrderType=OrderType,
        TradeType=TradeType,
        PositionAction=PositionAction,
    )
    module(
        "hummingbot.connector.trading_rule",
        TradingRule=lambda pair, **kw: SimpleNamespace(trading_pair=pair, **kw),
    )
    module("hummingbot.core.data_type.funding_info", FundingInfo=SimpleNamespace)
    module("hummingbot.core.data_type.trade_fee", TradeFeeSchema=SimpleNamespace)
    module("hummingbot.client.config.config_data_types", BaseConnectorConfigMap=BaseModel)
    module("bidict", bidict=SymbolMap)
    path = Path(__file__).parents[1] / "connectors/hummingbot/glyde_perpetual"
    spec = importlib.util.spec_from_file_location(
        "glyde_connector_under_test", path / "__init__.py"
    )
    package = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, package)
    spec.loader.exec_module(package)
    result = {}
    for name in (
        "wire",
        "auth",
        "user_stream_data_source",
        "api_order_book_data_source",
        "derivative",
        "utils",
    ):
        full = f"{spec.name}.glyde_perpetual_{name}"
        child = importlib.util.spec_from_file_location(full, path / f"glyde_perpetual_{name}.py")
        loaded = importlib.util.module_from_spec(child)
        monkeypatch.setitem(sys.modules, full, loaded)
        child.loader.exec_module(loaded)
        result[name] = loaded
    return SimpleNamespace(**result)
