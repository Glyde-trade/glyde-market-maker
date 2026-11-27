"""A busy consumer may skip replaceable live events but never fills."""

import asyncio

import pytest

from glyde_mm.client import OneApiError
from glyde_mm.config import Config
from glyde_mm.live_state import MAX_UNDELIVERED_FILLS, LiveStateClient


def book(n):
    return {"channel": "l2Book", "data": {"n": n}}


def full_client():
    live = LiveStateClient(object(), Config())  # type: ignore[arg-type]
    n = 0
    while not live._events.full():
        live._emit(book(n))
        n += 1
    return live


async def drain(live, stream):
    frames = []
    while not live._events.empty():
        frames.append(await anext(stream))
    return frames


def consumer(live):
    live._receive = lambda: asyncio.sleep(3600)  # Frames are emitted by the test itself.
    return live.stream()


def test_full_queue_skips_books_and_empty_owner_cuts_coalesce():
    live = full_client()
    live._emit(book("late"))
    live._emit({"channel": "mmOwner", "fills": []})
    live._emit({"channel": "mmOwner", "fills": []})
    owner = [f for f in live._events._queue if f["channel"] == "mmOwner"]  # type: ignore[attr-defined]
    assert live._events.full() and len(owner) == 1
    assert book("late") not in live._events._queue  # type: ignore[attr-defined]


async def test_fills_during_a_stalled_requote_are_delivered_not_fatal():
    # 27 Nov 2026: a requote stalled the consumer for ~10s while ~21 books/s arrived, then a
    # fill landed and the run ended with "live stream consumer overflow".
    live = full_client()
    for n in range(200):
        live._emit(book(f"stalled-{n}"))
    live._emit({"channel": "mmOwner", "fills": ["fill-1"]})
    live._emit(book("after"))
    live._emit({"channel": "mmOwner", "fills": ["fill-2", "fill-3"]})
    stream = consumer(live)
    frames = await drain(live, stream)
    owner = [f for f in frames if f["channel"] == "mmOwner"]
    assert owner == [{"channel": "mmOwner", "fills": ["fill-1", "fill-2", "fill-3"]}]
    assert frames[0] == book(1)  # The oldest book made room for the owner cut.
    live._emit({"channel": "mmOwner", "fills": ["fill-4"]})
    assert await drain(live, stream) == [{"channel": "mmOwner", "fills": ["fill-4"]}]
    await stream.aclose()
    await live.close()


def test_unbounded_fill_backlog_still_fails_closed():
    live = full_client()
    live._emit({"channel": "mmOwner", "fills": ["f"] * MAX_UNDELIVERED_FILLS})
    with pytest.raises(OneApiError, match="fill backlog exceeded"):
        live._emit({"channel": "mmOwner", "fills": ["one more"]})
