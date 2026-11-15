"""Loopback-only public HTTP/WS adapter for the offline container acceptance test."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from aiohttp import WSMsgType, web

from .server import FakeOneApiServer


class LocalOneApi:
    def __init__(self) -> None:
        self.fake = FakeOneApiServer(timestamp_ms=time.time_ns() // 1_000_000)
        self.fake.set_book("BTC", bids=[("99", "2")], asks=[("101", "2")])
        self.forbidden: list[str] = []
        self.sockets: set[web.WebSocketResponse] = set()
        self.runner: web.AppRunner | None = None
        self.producer: asyncio.Task[None] | None = None
        self.base_url = ""

    async def __aenter__(self) -> LocalOneApi:
        app = web.Application(client_max_size=16384)
        app.router.add_post("/v1/info", self.info)
        app.router.add_get("/v1/ws", self.websocket)
        app.router.add_route("*", "/{path:.*}", self.refuse)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        port = self.runner.addresses[0][1]
        self.base_url = f"http://127.0.0.1:{port}/v1"
        self.producer = asyncio.create_task(self.feed())
        return self

    async def __aexit__(self, *args: object) -> None:
        if self.producer is not None:
            self.producer.cancel()
            await asyncio.gather(self.producer, return_exceptions=True)
        for socket in tuple(self.sockets):
            await socket.close()
        if self.runner is not None:
            await self.runner.cleanup()

    async def refuse(self, request: web.Request) -> web.Response:
        self.forbidden.append(request.path)
        return web.json_response({"error": "public smoke fixture only"}, status=403)

    async def info(self, request: web.Request) -> web.Response:
        body: Any = await request.json()
        if (
            not isinstance(body, dict)
            or body.get("type") not in {"meta", "l2Book"}
            or "X-Glyde-API-Key" in request.headers
        ):
            return await self.refuse(request)
        self.fake.requests.append({"path": request.path, "body": body})
        return web.json_response(self.fake.info(body))

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse(max_msg_size=16384)
        await socket.prepare(request)
        self.sockets.add(socket)
        async with self.fake.connect_ws() as upstream:

            async def send() -> None:
                async for message in upstream:
                    await socket.send_str(message)

            sender = asyncio.create_task(send())
            try:
                async for message in socket:
                    if message.type == WSMsgType.TEXT:
                        import json

                        frame = json.loads(message.data)
                        kind = frame.get("subscription", {}).get("type")
                        if frame.get("method") != "ping" and kind not in {"l2Book", "trades"}:
                            self.forbidden.append("private websocket")
                            await socket.close(code=1008)
                            break
                        await upstream.send(message.data)
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                self.sockets.discard(socket)
        return socket

    async def feed(self) -> None:
        index = 0
        while True:
            await asyncio.sleep(0.25)
            self.fake.advance_time(max(1, time.time_ns() // 1_000_000 - self.fake.now_ms))
            self.fake.set_book("BTC", bids=[("99", "2")], asks=[("101", "2")])
            self.fake.market_order("BTC", buy=index % 2 == 0, size="0.1")
            index += 1
