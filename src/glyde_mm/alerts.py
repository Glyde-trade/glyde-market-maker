"""Opt-in, bounded best-effort chat notifications after trading cleanup."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx

from .config import Config
from .risk import RiskViolation

DELIVERY_SECONDS = 8
_PRIVATE_DELIVERY: ContextVar[bool] = ContextVar("private_webhook_delivery", default=False)
_HTTP_LOGGERS = (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
_EVENT_TEXT = {
    "risk_stop": "Risk checks stopped the session.",
    "unknown_order_outcome": (
        "Order/cancel outcome unconfirmed. Reconcile original IDs; do not retry."
    ),
    "daily_loss_breach": "Daily equity-loss threshold reached. Positions are not flattened.",
    "session_end": "Session ended; check local reports and venue state after any cleanup failure.",
}


class _PrivateHTTPLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_DELIVERY.get()


@contextmanager
def _private_http_logs() -> Iterator[None]:
    # HTTP diagnostics can include a webhook token in the URL or reflected headers.
    token = _PRIVATE_DELIVERY.set(True)
    private = _PrivateHTTPLogs()
    loggers = [logging.getLogger(name) for name in _HTTP_LOGGERS]
    for logger in loggers:
        logger.addFilter(private)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(private)
        _PRIVATE_DELIVERY.reset(token)


def _events(error: BaseException | None, risk_reasons: list[str]) -> list[str]:
    events = []
    for reason in risk_reasons:
        if reason in {"order_outcome_unknown", "cancel_outcome_unknown", "daily_loss"}:
            events.append("risk_stop")
        if reason == "daily_loss":
            events.append("daily_loss_breach")
        if reason in {"order_outcome_unknown", "cancel_outcome_unknown"}:
            events.append("unknown_order_outcome")
    seen = set()
    for _ in range(8):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        if isinstance(error, RiskViolation):
            events.append("risk_stop")
            reason = str(error)
            if reason == "daily_loss":
                events.append("daily_loss_breach")
            if reason in {
                "order_outcome_unknown",
                "cancel_outcome_unknown",
                "order_identity_mismatch",
                "cancel_identity_mismatch",
            }:
                events.append("unknown_order_outcome")
        error = error.__cause__ or error.__context__
    return list(dict.fromkeys(events)) + ["session_end"]


def _endpoint(config: Config) -> str | None:
    value = os.environ.get(config.alert_webhook_env, "")
    try:
        parsed = urlsplit(value)
        if (
            not value
            or len(value) > 4096
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.scheme not in {"https", "http"}
            or (
                parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            return None
        return str(httpx.URL(value))
    except (ValueError, httpx.InvalidURL):
        return None


async def _send(client: httpx.AsyncClient, endpoint: str, payload: dict[str, str]) -> bool:
    for attempt in range(3):
        try:
            response = await client.post(endpoint, json=payload)
            if 200 <= response.status_code < 300:
                return True
            if response.status_code != 429 and response.status_code < 500:
                return False
        except httpx.HTTPError:
            pass
        if attempt < 2:
            await asyncio.sleep(0.2 * 2**attempt)
    return False


def _warn(report: Callable[[str], object], message: str) -> None:
    try:
        report(message)
    except Exception:
        try:
            print("Chat alert status unavailable; inspect local reports.", file=sys.stderr)
        except Exception:
            # Broken diagnostic channels must not replace the trading outcome.
            return


async def notify_session(
    config: Config,
    run_id: str,
    outcome: str,
    error: BaseException | None,
    *,
    report: Callable[[str], object] = print,
    transport: httpx.AsyncBaseTransport | None = None,
    risk_reasons: list[str] | None = None,
) -> None:
    if not config.alerts_enabled:
        return
    endpoint = _endpoint(config)
    if endpoint is None:
        _warn(report, "Chat alerts unavailable: check the private webhook environment variable.")
        return
    delivered = True
    try:
        async with asyncio.timeout(DELIVERY_SECONDS):
            with _private_http_logs():
                async with httpx.AsyncClient(
                    timeout=1.5,
                    follow_redirects=False,
                    trust_env=False,
                    transport=transport,
                ) as client:
                    for event in _events(error, risk_reasons or []):
                        # Payloads contain no exception bodies, URLs, keys, balances or order IDs.
                        payload = {
                            "text": (
                                f"GLYDE-MM {event} ({config.mode}, {outcome}): {_EVENT_TEXT[event]}"
                            ),
                            "event": event,
                            "mode": config.mode,
                            "run_id": run_id,
                            "outcome": outcome,
                        }
                        delivered = await _send(client, endpoint, payload) and delivered
    except Exception:
        delivered = False
    if not delivered:
        _warn(
            report,
            "Chat alert delivery incomplete; trading outcome is unchanged. Check local reports.",
        )
