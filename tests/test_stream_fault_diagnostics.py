"""A live stream failure names its kind, frame, sequences, close code and raw cause (27 Nov 2026).

Live runs exited with a bare "live stream invalid, gapped or disconnected" that hid the reason.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from live_wire import batch, owner_roots
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from glyde_mm.client import (
    OneApiError,
    StreamDisconnected,
    reconnectable,
    socket_closed,
    subscription_refusal,
)
from glyde_mm.config import Config, RiskConfig
from glyde_mm.live_state import LiveStateClient, StreamFault
from glyde_mm.runlog import RunLog

OWNER = "0x" + "1" * 40


def state() -> LiveStateClient:
    config = Config(
        mode="live",
        owner=OWNER,
        vault_address="0x" + "4" * 40,
        risk=RiskConfig(max_position=D(1), max_order_notional=D(100)),
    )
    return LiveStateClient(None, config)  # type: ignore[arg-type]


def install(live: LiveStateClient) -> None:
    for frame in batch("mm-owner", owner_roots(OWNER), owner=OWNER):
        live.consume(frame)


def test_sequence_gap_names_group_and_expected_versus_received_sequence():
    live = state()
    install(live)
    with pytest.raises(OneApiError, match="continuity gap"):
        live.consume(batch("mm-owner", owner_roots(OWNER), sequence=4, previous=3, owner=OWNER)[0])
    failure = live._failure
    assert isinstance(failure, StreamFault) and isinstance(failure, StreamDisconnected)
    assert reconnectable(failure)
    assert (failure.kind, failure.group, failure.expected, failure.received) == (
        "gapped",
        "mm-owner",
        2,
        4,
    )
    assert str(failure) == (
        "live stream gapped (OneApiError: live stream continuity gap; channel "
        "clearinghouseState; group mm-owner; expected sequence 2, received 4)"
    )


def test_malformed_frame_keeps_the_raw_validation_error_and_frame_type():
    live = state()
    with pytest.raises(KeyError):
        live.consume({"channel": "notice", "data": {}})
    failure = live._failure
    assert type(failure) is StreamFault and not reconnectable(failure)
    assert failure.kind == "invalid" and failure.channel == "notice"
    assert str(failure) == "live stream invalid (KeyError: 'glyde'; channel notice)"
    assert failure.fields()["cause"] == "KeyError" and failure.fields()["reason"] is None


def test_protocol_rejection_logs_its_constant_reason():
    live = state()
    install(live)
    roots = owner_roots(OWNER, updates=[{"order": {"oid": 7}, "status": "mystery"}])
    with pytest.raises(OneApiError, match="unknown streamed order status"):
        for frame in batch("mm-owner", roots, sequence=2, previous=1, owner=OWNER):
            live.consume(frame)
    assert live._failure.fields() == {
        "kind": "invalid",
        "cause": "OneApiError",
        "reason": "unknown streamed order status",
        "channel": "glydeBatchEnd",
        "group": "mm-owner",
        "expected": 2,
        "received": 2,
        "close_code": None,
    }


def test_socket_close_reports_code_and_reason_or_missing_close_frame():
    closed = socket_closed(ConnectionClosedError(Close(1008, "lease_expired"), None))
    assert str(closed) == "WebSocket disconnected (close 1008 lease_expired)"
    assert (closed.close_code, closed.close_reason) == (1008, "lease_expired")
    fault = StreamFault("disconnected", closed)
    assert fault.close_code == 1008 and fault.fields()["close_code"] == 1008
    assert str(socket_closed(ConnectionClosedError(None, None))) == (
        "WebSocket disconnected (no close frame)"
    )
    assert str(socket_closed(ConnectionResetError())) == (
        "WebSocket disconnected (ConnectionResetError)"
    )
    # A close reason that could carry anything but a short constant is dropped.
    hidden = socket_closed(ConnectionClosedError(Close(1011, "key=0x" + "a" * 40 + "\n"), None))
    assert hidden.close_reason is None and "0x" not in str(hidden)


def test_refused_subscription_names_its_reason_code_and_constant_text():
    error = subscription_refusal(
        {"channel": "error", "reasonCode": "SUBSCRIPTION_CAPACITY", "data": "Glyde: too many"}
    )
    assert str(error) == "WebSocket subscription refused (SUBSCRIPTION_CAPACITY: Glyde: too many)"
    secret = subscription_refusal({"channel": "error", "data": "token 0xabc"})
    assert str(secret) == "WebSocket subscription refused (UNSPECIFIED)"


def test_run_log_records_stream_faults_but_never_free_text(tmp_path):
    live = state()
    with pytest.raises(KeyError):
        live.consume({"channel": "notice", "data": {}})
    with RunLog(tmp_path / "logs", mode="live", strategy="pure_market_making") as log:
        log.emit("stream_fault", **live._failure.fields())
        for bad in (
            {"reason": "secret 0xabc with Capitals"},
            {"channel": "a b"},
            {"kind": "weird"},
            {"expected": -1},
        ):
            with pytest.raises(ValueError):
                log.emit("stream_fault", **{**live._failure.fields(), **bad})
        text = log.path.read_text()
    assert '"event":"stream_fault"' in text and '"cause":"KeyError"' in text
    assert "secret" not in text
