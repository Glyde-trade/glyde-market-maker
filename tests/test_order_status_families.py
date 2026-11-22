"""Streamed order statuses follow HL's terminal families instead of a fixed list."""

import pytest

from glyde_mm.live_state import LIVE_ORDER_STATUSES, terminal_order_status


@pytest.mark.parametrize(
    "status",
    [
        "filled",
        "canceled",
        "rejected",
        "expired",
        "internalCancel",
        "scheduledCancel",
        "marginCanceled",
        "reduceOnlyCanceled",
        "liquidatedCanceled",
        "selfTradeCanceled",
        "tickRejected",
        "minTradeNtlRejected",
        "perpMarginRejected",
    ],
)
def test_terminal_families(status):
    assert terminal_order_status(status)
    assert status not in LIVE_ORDER_STATUSES


@pytest.mark.parametrize("status", ["open", "triggered", "pending"])
def test_live_statuses_are_not_terminal(status):
    assert not terminal_order_status(status)
    assert status in LIVE_ORDER_STATUSES


@pytest.mark.parametrize("status", ["mystery", "", None, 3, "Canceledx"])
def test_unknown_statuses_are_neither(status):
    assert not terminal_order_status(status)
    assert status not in LIVE_ORDER_STATUSES
