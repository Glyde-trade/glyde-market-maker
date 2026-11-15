"""Socket-free JSON WebSocket connection for the public testing harness."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .server import FakeOneApiServer


class FakeWebSocket:
    """Async context manager with the send/recv/iteration API used by websockets."""

    def __init__(self, server: FakeOneApiServer) -> None:
        self.server = server
        self.subscriptions: list[dict[str, Any]] = []
        self.closed = False
        self._messages: asyncio.Queue[str | None] = asyncio.Queue()

    async def __aenter__(self) -> FakeWebSocket:
        if self.closed:
            raise ConnectionError("fake websocket is closed")
        self.server._connections.add(self)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def send(self, message: str | bytes) -> None:
        if self.closed:
            raise ConnectionError("fake websocket is closed")
        request = json.loads(message)
        method = request.get("method")
        if method == "ping":
            self.push({"channel": "pong"})
            return
        subscription = request.get("subscription", {})
        self.server._validate_subscription(subscription)
        if method == "subscribe":
            self.push({"channel": "subscriptionResponse", "data": request})
            if subscription not in self.subscriptions:
                self.subscriptions.append(dict(subscription))
                self.server._snapshot(self, subscription)
        elif method == "unsubscribe":
            self.subscriptions = [s for s in self.subscriptions if s != subscription]
            self.push({"channel": "subscriptionResponse", "data": request})
        else:
            raise ValueError("unsupported websocket method")

    def push(self, message: dict[str, Any]) -> None:
        if not self.closed:
            self._messages.put_nowait(json.dumps(message))

    async def recv(self) -> str:
        if self.closed:
            raise ConnectionError("fake websocket is closed")
        message = await self._messages.get()
        if message is None:
            raise ConnectionError("fake websocket disconnected")
        return message

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        try:
            return await self.recv()
        except ConnectionError as error:
            raise StopAsyncIteration from error

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason
        self.closed = True
        self.server._connections.discard(self)
        self._messages.put_nowait(None)
