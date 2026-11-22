"""A busy consumer may skip replaceable live events but never fills."""

import pytest

from glyde_mm.client import OneApiError
from glyde_mm.config import Config
from glyde_mm.live_state import LiveStateClient


def full_client():
    live = LiveStateClient(object(), Config())  # type: ignore[arg-type]
    while not live._events.full():
        live._events.put_nowait({"channel": "mmOwner", "fills": []})
    return live


def test_full_queue_skips_books_and_empty_owner_cuts():
    live = full_client()
    live._emit({"channel": "l2Book", "data": {}})
    live._emit({"channel": "mmOwner", "fills": []})
    assert live._events.full()


def test_full_queue_still_refuses_to_drop_fills():
    live = full_client()
    with pytest.raises(OneApiError, match="consumer overflow"):
        live._emit({"channel": "mmOwner", "fills": [{"tid": 1}]})
