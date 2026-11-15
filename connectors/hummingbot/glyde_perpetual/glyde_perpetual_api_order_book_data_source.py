# Copyright 2026 Glyde. Apache-2.0; adapted from Hummingbot hyperliquid_perpetual.
"""Reuse upstream book handling without using upstream venue URLs or zero funding fallbacks."""

from decimal import Decimal

from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_api_order_book_data_source import (  # noqa: E501
    HyperliquidPerpetualAPIOrderBookDataSource,
)
from hummingbot.core.data_type.funding_info import FundingInfo


class GlydePerpetualAPIOrderBookDataSource(HyperliquidPerpetualAPIOrderBookDataSource):
    async def _connected_websocket_assistant(self):
        ws = await self._api_factory.get_ws_assistant()
        await ws.connect(
            ws_url=self._connector._glyde_settings.ws_url, ping_timeout=30, message_timeout=30
        )
        return ws

    async def get_funding_info(self, trading_pair):
        coin = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        response = await self._connector._api_post("/info", data={"type": "metaAndAssetCtxs"})
        for meta, context in zip(response[0]["universe"], response[1], strict=True):
            if meta["name"] == coin:
                return FundingInfo(
                    trading_pair=trading_pair,
                    index_price=Decimal(context["oraclePx"]),
                    mark_price=Decimal(context["markPx"]),
                    rate=Decimal(context["funding"]),
                    next_funding_utc_timestamp=self._next_funding_time(),
                )
        raise OSError("Glyde funding context unavailable")
