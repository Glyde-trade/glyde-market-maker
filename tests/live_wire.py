"""Synthetic One API sequenced frames shared by offline runner fixtures."""

import time
from copy import deepcopy

OWNER = ("clearinghouseState", "openOrders", "orderUpdates", "userFills")


def batch(
    group,
    data,
    *,
    sequence=1,
    previous=None,
    epoch="epoch",
    session="session",
    owner=None,
    kind=None,
    stamp=None,
):
    kind = kind or ("snapshot" if previous is None else "delta")
    parts = [
        {"index": i, "subscriptionId": f"s-{channel}", "channel": channel}
        for i, channel in enumerate(data)
    ]
    meta = {
        "version": 1,
        "group": group,
        "source": "engine" if owner else "market",
        "owner": owner,
        "session": session,
        "epoch": epoch,
        "sequence": str(sequence),
        "previous": str(previous) if previous is not None else None,
        "cursor": {"command": str(sequence), "transaction": str(sequence)},
        "schemaVersion": "1",
        "riskVersion": "1",
        "feeVersion": "1",
        "batch": {"id": f"{group}-{sequence}", "kind": kind, "parts": parts, "index": None},
    }
    frames = []
    for i, (channel, value) in enumerate(data.items()):
        current = deepcopy(meta)
        current["batch"]["index"] = i
        frames.append({"channel": channel, "data": deepcopy(value), "glyde": current})
    frames.append({"channel": "glydeBatchEnd", "data": {}, "glyde": deepcopy(meta)})
    return frames


def owner_roots(
    owner, *, orders=None, fills=None, updates=None, stamp=None, equity="1000", position="0"
):
    return {
        "clearinghouseState": {
            "user": owner,
            "clearinghouseState": {
                "marginSummary": {"accountValue": equity},
                "withdrawable": equity,
                "assetPositions": []
                if position == "0"
                else [{"position": {"coin": "BTC", "szi": position}}],
                "time": stamp if stamp is not None else time.time_ns() // 1_000_000,
            },
        },
        "openOrders": {"user": owner, "orders": orders or []},
        "orderUpdates": updates or [],
        "userFills": {"user": owner, "isSnapshot": False, "fills": fills or []},
    }
