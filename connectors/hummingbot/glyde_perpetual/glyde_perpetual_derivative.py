# Copyright 2026 Glyde. Apache-2.0; adapted from Hummingbot hyperliquid_perpetual.
"""Hummingbot adapter; private identity and signing namespace are separate."""

from decimal import Decimal

from bidict import bidict
from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_derivative import (  # noqa: E501
    HyperliquidPerpetualDerivative,
)
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import OrderType, PositionAction, TradeType

from .glyde_perpetual_api_order_book_data_source import GlydePerpetualAPIOrderBookDataSource
from .glyde_perpetual_auth import GlydePerpetualAuth
from .glyde_perpetual_user_stream_data_source import GlydePerpetualUserStreamDataSource
from .glyde_perpetual_wire import Settings, market_rules, quantize_price, wire_number


class GlydePerpetualDerivative(HyperliquidPerpetualDerivative):
    def __init__(
        self,
        glyde_perpetual_owner,
        glyde_perpetual_vault_address,
        glyde_perpetual_private_key,
        glyde_perpetual_read_credential,
        glyde_perpetual_base_url="https://api.glyde.trade/v1",
        glyde_perpetual_mainnet=True,
        balance_asset_limit=None,
        rate_limits_share_pct=Decimal("100"),
        trading_pairs=None,
        trading_required=True,
    ):
        self._glyde_settings = Settings(
            glyde_perpetual_owner,
            glyde_perpetual_vault_address,
            glyde_perpetual_private_key,
            glyde_perpetual_read_credential,
            glyde_perpetual_base_url,
            glyde_perpetual_mainnet,
        )
        self._glyde_auth = GlydePerpetualAuth(self._glyde_settings)
        self._glyde_suspended = False
        self.unresolved_exchange_envelope = None
        self._glyde_sizes = {}
        super().__init__(
            balance_asset_limit=balance_asset_limit,
            rate_limits_share_pct=rate_limits_share_pct,
            hyperliquid_perpetual_address=glyde_perpetual_owner,
            hyperliquid_perpetual_secret_key=glyde_perpetual_private_key,
            use_vault=True,
            hyperliquid_perpetual_mode="api_wallet",
            trading_pairs=trading_pairs,
            trading_required=trading_required,
            domain="glyde_perpetual",
            enable_hip3_markets=False,
        )

    @property
    def authenticator(self):
        return self._glyde_auth

    @property
    def ready(self):
        return not self._glyde_suspended and super().ready

    def _unknown_exchange(self):
        self._glyde_suspended = True
        if self.unresolved_exchange_envelope is None:
            self.unresolved_exchange_envelope = self._glyde_auth.wire.last_envelope

    async def _api_request_url(self, path_url, is_auth_required=False):
        return self._glyde_settings.url(path_url)

    async def _api_request(self, path_url, **kwargs):
        if kwargs.get("overwrite_url") is not None:
            raise ValueError("endpoint overrides are not permitted")
        if path_url not in {"/info", "/exchange"}:
            raise ValueError("unsupported One API path")
        private_read = False
        if path_url == "/info":
            body, _ = self._glyde_auth.wire.info(kwargs.get("data", {}))
            kwargs["data"] = body
            if "user" in body:
                private_read = True
                kwargs["is_auth_required"] = True
        else:
            if self._glyde_suspended and kwargs.get("data", {}).get("type") not in {
                "cancel",
                "cancelByCloid",
            }:
                raise OSError("Glyde trading suspended; reconcile original envelope")
            kwargs["is_auth_required"] = True
        try:
            result = await super()._api_request(path_url=path_url, **kwargs)
            if private_read and isinstance(result, dict) and result.get("status") == "err":
                raise OSError("Glyde private read refused")
            return result
        except Exception:
            if path_url == "/exchange":
                self._unknown_exchange()
            elif private_read:
                self._glyde_suspended = True
            raise OSError("Glyde request refused or outcome unknown; no automatic retry") from None

    async def _ensure_builder_fee_resolved(self):
        # Glyde does not add Hyperliquid builder actions or fees to signed orders.
        return None

    def _build_builder_field(self):
        return None

    async def _uses_spot_balances(self):
        return False

    async def _make_trading_rules_request(self):
        return [await self._api_post("/info", data={"type": "meta"}), []]

    async def _make_trading_pairs_request(self):
        return await self._make_trading_rules_request()

    async def _initialize_trading_pair_symbol_map(self):
        info = await self._make_trading_rules_request()
        self._initialize_trading_pair_symbols_from_exchange_info(info)

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info):
        meta = exchange_info[0]
        self._set_trading_pair_symbol_map(
            bidict({name: f"{name}-USD" for name, _, _, _ in market_rules(meta)})
        )

    async def _update_trading_rules(self):
        info = await self._make_trading_rules_request()
        self._initialize_trading_pair_symbols_from_exchange_info(info)
        rules = await self._format_trading_rules(info)
        self._trading_rules = {rule.trading_pair: rule for rule in rules}

    async def _format_trading_rules(self, exchange_info_dict):
        contexts = exchange_info_dict[1] or None
        rules = market_rules(exchange_info_dict[0], contexts)
        self.coin_to_asset = {name: asset for name, asset, _, _ in rules}
        self._is_hip3_market = {name: False for name in self.coin_to_asset}
        self._glyde_sizes = {
            row["name"]: row["szDecimals"] for row in exchange_info_dict[0]["universe"]
        }
        result = []
        for name, _, size_step, price_step in rules:
            pair = await self.trading_pair_associated_to_exchange_symbol(symbol=name)
            result.append(
                TradingRule(
                    pair,
                    min_base_amount_increment=size_step,
                    min_price_increment=price_step,
                    min_order_size=size_step,
                    min_notional_size=Decimal("10"),
                    buy_order_collateral_token="USD",
                    sell_order_collateral_token="USD",
                )
            )
        return result

    def quantize_order_price(self, trading_pair, price):
        coin = self._trading_pair_symbol_map.inverse[trading_pair]
        return quantize_price(price, self._glyde_sizes[coin])

    async def _place_order(
        self,
        order_id,
        trading_pair,
        amount,
        trade_type,
        order_type,
        price,
        position_action=PositionAction.NIL,
        **kwargs,
    ):
        if self._glyde_suspended:
            raise OSError("Glyde trading suspended; reconcile and restart explicitly")
        coin = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        tif = {OrderType.LIMIT: "Gtc", OrderType.LIMIT_MAKER: "Alo", OrderType.MARKET: "Ioc"}[
            order_type
        ]
        action = {
            "type": "order",
            "orders": [
                {
                    "a": self.coin_to_asset[coin],
                    "b": trade_type is TradeType.BUY,
                    "p": wire_number(price),
                    "s": wire_number(amount),
                    "r": position_action == PositionAction.CLOSE,
                    "t": {"limit": {"tif": tif}},
                    "c": order_id,
                }
            ],
            "grouping": "na",
        }
        result = await self._api_post("/exchange", data=action, is_auth_required=True)
        if isinstance(result, dict) and result.get("status") == "err":
            raise OSError("Glyde refused order")
        try:
            if result["status"] != "ok" or result["response"]["type"] != "order":
                raise ValueError
            statuses = result["response"]["data"]["statuses"]
            if len(statuses) != 1 or not isinstance(statuses[0], dict) or len(statuses[0]) != 1:
                raise ValueError
            status = statuses[0]
            if "error" in status:
                raise OSError("Glyde refused order")
            order = status.get("resting") or status.get("filled")
            oid = order["oid"]
            if (
                isinstance(oid, bool)
                or not str(oid).isascii()
                or not str(oid).isdigit()
                or int(oid) <= 0
            ):
                raise ValueError
            return str(oid), self.current_timestamp
        except (KeyError, TypeError, ValueError):
            self._unknown_exchange()
            raise OSError("Glyde order outcome unknown; reconcile original envelope") from None

    async def _place_cancel(self, order_id, tracked_order):
        coin = await self.exchange_symbol_associated_to_pair(
            trading_pair=tracked_order.trading_pair
        )
        result = await self._api_post(
            "/exchange",
            data={
                "type": "cancelByCloid",
                "cancels": [{"asset": self.coin_to_asset[coin], "cloid": order_id}],
            },
            is_auth_required=True,
        )
        try:
            if result["status"] != "ok":
                raise ValueError
            if result["response"]["data"]["statuses"] != ["success"]:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            self._unknown_exchange()
            raise OSError("Glyde cancellation unconfirmed; reconcile original envelope") from None
        return True

    def _create_user_stream_data_source(self):
        return GlydePerpetualUserStreamDataSource(
            auth=self._auth,
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    def _create_order_book_data_source(self):
        return GlydePerpetualAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )
