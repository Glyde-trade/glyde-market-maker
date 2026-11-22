"""Socket admission refusals back off within the first-book window and fail cleanly."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from glyde_mm import client as client_module
from glyde_mm.client import OneApiClient, OneApiHTTPError
from glyde_mm.config import Config

BOOK = json.dumps(
    {
        "channel": "l2Book",
        "data": {
            "coin": "BTC",
            "time": 1700000000000,
            "levels": [[{"px": "59000", "sz": "1", "n": 1}], [{"px": "59001", "sz": "1", "n": 1}]],
        },
    }
)


def refused(status: int, retry_after: str | None = None) -> InvalidStatus:
    headers = Headers({"Retry-After": retry_after} if retry_after is not None else {})
    return InvalidStatus(Response(status, "refused", headers))


class OneFrameSocket:
    def __init__(self):
        self.frames = [BOOK]

    async def send(self, value):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop()


def scripted_connect(outcomes):
    attempts = []

    @asynccontextmanager
    async def connect(url, **kwargs):
        attempts.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        yield outcome

    return connect, attempts


@pytest.fixture
def sleeps(monkeypatch):
    waited = []

    async def fake_sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    return waited


async def first_frame(connect):
    async with OneApiClient(Config(), connect_ws=connect) as client:
        stream = client.stream([{"type": "l2Book", "coin": "BTC"}])
        try:
            return await anext(stream)
        finally:
            await stream.aclose()


async def test_429_admission_backs_off_honoring_retry_after_then_streams(sleeps):
    connect, attempts = scripted_connect([refused(429, "2"), refused(429), OneFrameSocket()])
    frame = await first_frame(connect)
    assert frame["channel"] == "l2Book"
    assert len(attempts) == 3
    assert sleeps == [2.0, 2.0]


async def test_429_admission_gives_up_with_sanitized_error_after_window(monkeypatch, sleeps):
    monkeypatch.setattr(client_module, "SOCKET_ADMISSION_SECONDS", 0.0)
    connect, attempts = scripted_connect([refused(429, "1")])
    with pytest.raises(OneApiHTTPError) as error:
        await first_frame(connect)
    assert error.value.status_code == 429 and len(attempts) == 1 and sleeps == []


async def test_non_429_refusal_is_not_retried(sleeps):
    connect, attempts = scripted_connect([refused(403)])
    with pytest.raises(OneApiHTTPError) as error:
        await first_frame(connect)
    assert error.value.status_code == 403 and len(attempts) == 1 and sleeps == []


def test_cli_names_sanitized_refusal_cause(tmp_path, monkeypatch, capsys):
    from glyde_mm import cli
    from glyde_mm.risk import RiskViolation

    async def busy(config, **kwargs):
        raise RiskViolation("local_session_busy")

    monkeypatch.setattr(cli, "run_strategy", busy)
    path = tmp_path / "bot.yaml"
    path.write_text("mode: paper\n")
    assert cli.main(["paper", str(path)]) == 1
    assert "Unable to run: RiskViolation: local_session_busy" in capsys.readouterr().err


def test_cli_hides_unsanitized_error_bodies(tmp_path, monkeypatch, capsys):
    from glyde_mm import cli

    async def leaky(config, **kwargs):
        raise OSError("/home/me/.secret-key-path")

    monkeypatch.setattr(cli, "run_strategy", leaky)
    path = tmp_path / "bot.yaml"
    path.write_text("mode: paper\n")
    assert cli.main(["paper", str(path)]) == 1
    err = capsys.readouterr().err
    assert "Unable to run (OSError)" in err and "secret-key-path" not in err
