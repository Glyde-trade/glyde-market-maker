"""One state writer: one in-flight snapshot, one coalesced successor, explicit barriers."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path


class RiskStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="glyde-risk-state")
        self._pending: tuple[str, Future[None]] | None = None
        self._latest: Future[None] | None = None
        self._running = False
        self._closed = False
        self._failure: OSError | None = None

    def check(self) -> None:
        with self._lock:
            if self._failure is not None:
                raise self._failure

    def submit(self, snapshot: str) -> None:
        with self._lock:
            if self._failure is not None:
                raise self._failure
            if self._closed:
                raise OSError("risk state writer is closed")
            future = self._pending[1] if self._pending is not None else Future[None]()
            self._pending = snapshot, future
            self._latest = future
            if not self._running:
                self._running = True
                try:
                    self._worker.submit(self._drain)
                except RuntimeError as error:
                    self._failure = OSError("risk state writer unavailable")
                    self._running = False
                    self._pending = None
                    future.set_exception(self._failure)
                    raise self._failure from error

    async def flush(self) -> None:
        with self._lock:
            future = self._latest
        if future is not None:
            barrier = asyncio.wrap_future(future)
            # Caller cancellation must not cancel a write or leave an unobserved error.
            barrier.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            await asyncio.shield(barrier)
        self.check()

    async def close(self) -> None:
        with self._lock:
            self._closed = True
        try:
            await self.flush()
        finally:
            self._worker.shutdown(wait=False)

    def _drain(self) -> None:
        while True:
            with self._lock:
                pending, self._pending = self._pending, None
                if pending is None:
                    self._running = False
                    return
            snapshot, future = pending
            try:
                self._write(snapshot)
            except BaseException as error:
                failure = (
                    error if isinstance(error, OSError) else OSError("risk state writer failed")
                )
                with self._lock:
                    self._failure = failure
                    successor, self._pending = self._pending, None
                    self._running = False
                future.set_exception(failure)
                if successor is not None:
                    successor[1].set_exception(failure)
                return
            future.set_result(None)

    def _write(self, snapshot: str) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.path.parent, delete=False
            ) as handle:
                temporary = handle.name
                handle.write(snapshot)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
