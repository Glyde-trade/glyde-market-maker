"""A busy consumer may skip replaceable live events but never fills."""

import pytest

from glyde_mm.client import OneApiError
from glyde_mm.config import Config
from glyde_mm.live_state import LiveStateClient


def book(coin: str, n: int) -> dict[str, object]:
    return {"channel": "l2Book", "data": {"coin": coin, "time": n}}


def full_client(*rows: dict[str, object]):
    live = LiveStateClient(object(), Config())  # type: ignore[arg-type]
    for row in rows:
        live._events.put_nowait(row)
    while not live._events.full():
        live._events.put_nowait({"channel": "mmOwner", "fills": [{"tid": 0}]})
    return live


def queued(live: LiveStateClient) -> list[object]:
    return list(live._events._queue)  # type: ignore[attr-defined]


def test_full_queue_skips_books_and_empty_owner_cuts():
    live = full_client()
    live._emit(book("BTC", 1))
    live._emit({"channel": "mmOwner", "fills": []})
    assert live._events.full()
    assert all(row["fills"] for row in queued(live))  # type: ignore[index]


def test_full_queue_of_fills_still_refuses_to_drop_a_fill():
    live = full_client()
    with pytest.raises(OneApiError, match="consumer overflow"):
        live._emit({"channel": "mmOwner", "fills": [{"tid": 1}]})


def test_fill_evicts_an_empty_owner_cut_before_any_book():
    cut = {"channel": "mmOwner", "fills": []}
    live = full_client(book("BTC", 1), cut)
    fill = {"channel": "mmOwner", "fills": [{"tid": 1}]}
    live._emit(fill)
    rows = queued(live)
    assert cut not in rows
    assert rows[0] == book("BTC", 1)
    assert rows[-1] == fill


def test_fill_evicts_a_superseded_book_and_keeps_the_latest_per_market():
    live = full_client(book("BTC", 1), book("ETH", 1), book("BTC", 2))
    fill = {"channel": "mmOwner", "fills": [{"tid": 1}]}
    live._emit(fill)
    rows = queued(live)
    assert book("BTC", 1) not in rows
    assert rows[:2] == [book("ETH", 1), book("BTC", 2)]
    assert rows[-1] == fill


def test_fill_evicts_the_oldest_book_when_none_is_superseded():
    live = full_client(book("BTC", 1), book("ETH", 1))
    fill = {"channel": "mmOwner", "fills": [{"tid": 1}]}
    live._emit(fill)
    rows = queued(live)
    assert rows[0] == book("ETH", 1)
    assert rows[-1] == fill


def test_disconnect_sentinel_is_never_evicted():
    live = LiveStateClient(object(), Config())  # type: ignore[arg-type]
    live._events.put_nowait(None)
    while not live._events.full():
        live._events.put_nowait({"channel": "mmOwner", "fills": [{"tid": 0}]})
    with pytest.raises(OneApiError, match="consumer overflow"):
        live._emit({"channel": "mmOwner", "fills": [{"tid": 1}]})
    assert queued(live)[0] is None
