"""Local runner coordination and a symbol-scoped cancellation of the bot's own orders."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager, AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from .client import OneApiClient
from .config import Config
from .models import CancelRequest, TradingClient, is_bot_order
from .risk import RiskGuard, RiskViolation


def _paths(config: Config, directory: Path) -> tuple[Path, Path, Path]:
    identity = (
        config.mode,
        config.base_url,
        config.owner,
        config.vault_address,
        sorted(config.symbols),
    )
    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    operation, session, request = (
        directory / f"control-{digest}.{kind}" for kind in ("operation", "run", "stop")
    )
    return operation, session, request


@contextmanager
def _lock(path: Path) -> Iterator[int]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RiskViolation("local_session_busy") from None
        yield descriptor
    finally:
        os.close(descriptor)


class RunnerControl:
    def __init__(self, config: Config, directory: Path, stop: asyncio.Event) -> None:
        self.operation, self.session, self.request = _paths(config, directory)
        self.stop = stop

    async def __aenter__(self) -> Self:
        with _lock(self.operation):
            self.ownership = _lock(self.session)
            self.ownership.__enter__()
            try:
                self.request.unlink(missing_ok=True)
            except BaseException:
                self.ownership.__exit__(None, None, None)
                raise
        self.monitor = asyncio.create_task(self._monitor())
        return self

    async def _monitor(self) -> None:
        while not self.stop.is_set():
            if self.request.exists():
                self.stop.set()
                return
            await asyncio.sleep(0.1)

    async def __aexit__(self, *args: object) -> None:
        self.monitor.cancel()
        await asyncio.gather(self.monitor, return_exceptions=True)
        self.ownership.__exit__(None, None, None)


async def _wait_for_runner(path: Path) -> AbstractContextManager[int]:
    while True:
        ownership = _lock(path)
        try:
            ownership.__enter__()
            return ownership
        except RiskViolation:
            await asyncio.sleep(0.05)


def _originals(config: Config, directory: Path) -> list[CancelRequest]:
    from .runner import risk_state_path

    path = risk_state_path(config, directory)
    if not path.exists():
        return []
    record = json.loads(path.read_text()).get("execution")
    if record is None:
        return []
    RiskGuard._validate_execution(record)
    return [
        CancelRequest(
            row["symbol"],
            order_id=row["order_id"],
            client_order_id=None if row["order_id"] else row["client_order_id"],
        )
        for row in record["originals"]
        if row["symbol"] in config.symbols
    ]


def _write_report(path: Path, result: dict[str, Any]) -> None:
    with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


async def stop_bot(
    config: Config,
    directory: Path,
    *,
    client_factory: Callable[[Config], AbstractAsyncContextManager[TradingClient]] = OneApiClient,
    wait_seconds: float = 15,
) -> tuple[dict[str, Any], Path]:
    operation, session, request = _paths(config, directory)
    result: dict[str, Any] = {
        "time": datetime.now(UTC).isoformat(),
        "mode": config.mode,
        "symbol": ",".join(config.symbols),
        "outcome": "failed",
        "remaining_orders": None,
        "positions_closed": False,
        "failure": None,
    }
    reports = directory / "stops"
    reports.mkdir(mode=0o700, exist_ok=True)
    path = reports / f"stop-{uuid4().hex}.json"
    try:
        with _lock(operation):
            descriptor = os.open(request, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
            async with asyncio.timeout(wait_seconds):
                ownership = await _wait_for_runner(session)
            try:
                if config.mode == "live":
                    async with asyncio.timeout(wait_seconds):
                        async with client_factory(config) as client:
                            failure = None
                            try:
                                originals = _originals(config, directory)
                                for original in originals:
                                    try:
                                        rows = await client.cancel_orders([original])
                                        if len(rows) != 1 or rows[0].status not in {
                                            "cancelled",
                                            "already_closed",
                                        }:
                                            raise RiskViolation("cancel_outcome_unknown")
                                        if rows[0].order_id not in (None, original.order_id) or (
                                            rows[0].client_order_id
                                            not in (None, original.client_order_id)
                                        ):
                                            raise RiskViolation("cancel_identity_mismatch")
                                    except Exception as error:
                                        failure = error
                            except Exception as error:
                                failure = error
                            for symbol in config.symbols:
                                try:
                                    await client.cancel_all(symbol)
                                except Exception as error:
                                    failure = failure or error
                            remaining = await client.open_orders()
                            # Untagged orders belong to the account holder and stay open.
                            result["remaining_orders"] = sum(
                                row.symbol in config.symbols and is_bot_order(row.client_order_id)
                                for row in remaining
                            )
                            if failure is not None or result["remaining_orders"]:
                                raise RiskViolation("cancel_outcome_unknown")
                else:
                    result["remaining_orders"] = 0
                result["outcome"] = "stopped"
            finally:
                ownership.__exit__(None, None, None)
    except Exception as error:
        # The report deliberately excludes exception text, credentials and account identifiers.
        result["failure"] = "timeout" if isinstance(error, TimeoutError) else "stop_unconfirmed"
    _write_report(path, result)
    return result, path
