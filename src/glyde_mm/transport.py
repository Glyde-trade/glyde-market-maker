"""Typed seams for injected transports; JSON is decoded at the client boundary."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, Self, TypeAlias

from .config import Config
from .models import Book, TradingClient

JsonObject: TypeAlias = dict[str, Any]


class WebSocket(Protocol):
    async def send(self, message: str) -> None: ...
    async def close(self) -> None: ...
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...


class MarketClient(TradingClient, Protocol):
    async def meta(self) -> JsonObject: ...
    async def book(self, symbol: str) -> Book: ...
    def stream(self, subscriptions: Sequence[JsonObject]) -> AsyncGenerator[JsonObject, None]: ...
    async def close(self) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *args: object) -> None: ...


ClientFactory: TypeAlias = Callable[[Config], MarketClient]
WebSocketFactory: TypeAlias = Callable[..., AbstractAsyncContextManager[WebSocket]]
