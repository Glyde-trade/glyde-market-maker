# Copyright 2026 Glyde. Apache-2.0; adapted from Hummingbot hyperliquid_perpetual.
"""Read-authenticated owner stream; never subscribe to stock Hyperliquid's user channel."""

import asyncio

from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_user_stream_data_source import (  # noqa: E501
    HyperliquidPerpetualUserStreamDataSource,
)
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest

from .glyde_perpetual_wire import number


class GlydePerpetualUserStreamDataSource(HyperliquidPerpetualUserStreamDataSource):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fills = {}

    async def _connected_websocket_assistant(self):
        settings = self._connector._glyde_settings
        ws = await self._get_ws_assistant()
        await ws.connect(
            ws_url=settings.ws_url,
            ping_timeout=30,
            message_timeout=30,
            ws_headers={"X-Glyde-API-Key": settings.read_credential},
        )
        self._ping_task = safe_ensure_future(self._ping_thread(ws))
        return ws

    async def _subscribe_channels(self, websocket_assistant):
        for channel in ("orderUpdates", "userFills"):
            await websocket_assistant.send(
                WSJSONRequest(
                    payload={
                        "method": "subscribe",
                        "subscription": {
                            "type": channel,
                            "user": self._connector._glyde_settings.owner,
                        },
                    }
                )
            )

    async def _process_event_message(self, event_message, queue):
        try:
            self._process_private_event(event_message, queue)
        except Exception:
            self._connector._glyde_suspended = True
            raise OSError(
                "Glyde private stream invalid; reconcile and restart explicitly"
            ) from None

    def _process_private_event(self, event_message, queue):
        channel = event_message.get("channel")
        if channel in {"error", "glydeSafety", "resnapshot_required"}:
            self._connector._glyde_suspended = True
            raise OSError("Glyde private stream refused or requires reconciliation")
        if channel == "orderUpdates":
            rows = event_message["data"]
            if not isinstance(rows, list) or len(rows) > 4096:
                raise OSError("invalid Glyde order update")
            queue.put_nowait(event_message)
        elif channel == "userFills":
            data = event_message["data"]
            if (
                data["user"].lower() != self._connector._glyde_settings.owner.lower()
                or type(data["isSnapshot"]) is not bool
                or not isinstance(data["fills"], list)
                or len(data["fills"]) > 4096
            ):
                raise OSError("Glyde fill owner or snapshot mismatch")
            fresh = []
            for fill in data["fills"]:
                if any(
                    isinstance(fill[k], bool)
                    or not str(fill[k]).isascii()
                    or not str(fill[k]).isdigit()
                    for k in ("oid", "tid", "time")
                ):
                    raise ValueError("invalid fill identity/time")
                if number(fill["px"]) <= 0 or number(fill["sz"]) <= 0:
                    raise ValueError("invalid fill economics")
                number(fill["fee"])
                if not isinstance(fill["coin"], str) or not isinstance(fill["dir"], str):
                    raise ValueError("invalid fill market/direction")
                identity = str(fill["oid"]), str(fill["tid"])
                if identity in self._fills:
                    if self._fills[identity] != fill:
                        raise OSError("conflicting Glyde fill identity")
                    continue
                if len(self._fills) >= 8192:
                    raise OSError("Glyde fill evidence budget exhausted")
                self._fills[identity] = dict(fill)
                fresh.append(fill)
            # Reuse upstream's tracked-order fill processor; only the internal queue is normalized.
            queue.put_nowait({"channel": "user", "data": {"fills": fresh}})

    async def _on_user_stream_interruption(self, websocket_assistant):
        self._connector._glyde_suspended = True
        await super()._on_user_stream_interruption(websocket_assistant)

    async def _ping_thread(self, websocket_assistant):
        while True:
            await asyncio.sleep(20)
            await websocket_assistant.send(WSJSONRequest(payload={"method": "ping"}))
