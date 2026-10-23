"""
Thin wrapper over `requests` that turns every failure into a typed error.

`send()` always passes a timeout and either returns a successful Response or
raises RateLimitError / TransientError (retryable) or APIError / AuthError
(not). Service clients add their own messages for statuses that mean
something specific to them (Jira 404 = ticket not found, ...) via `on_status`.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from docgen.errors import APIError, AuthError, DocgenError, RateLimitError, TransientError

log = logging.getLogger(__name__)

TRANSIENT_STATUS = {408, 425, 500, 502, 503, 504, 520, 522, 524, 529}
CONNECT_TIMEOUT = 10.0


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP date. Returns seconds, or None."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _excerpt(response: requests.Response, limit: int = 300) -> str:
    text = (response.text or "").strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def classify(response: requests.Response, service: str) -> DocgenError:
    """Map an unsuccessful response to the matching error type."""
    status = response.status_code
    detail = f"{service} returned HTTP {status}: {_excerpt(response)}"
    if status == 429:
        return RateLimitError(detail, service=service, status=status,
                              retry_after=parse_retry_after(response.headers.get("Retry-After")))
    if status in TRANSIENT_STATUS:
        return TransientError(detail, service=service, status=status)
    if status in (401, 403):
        return AuthError(detail, hint=f"Check the {service} credentials in your .env")
    return APIError(detail, service=service, status=status)


def send(
    method: str,
    url: str,
    *,
    service: str,
    timeout: float,
    on_status: Callable[[requests.Response], DocgenError | None] | None = None,
    unreachable_hint: str | None = None,
    session: requests.Session | None = None,
    **kwargs,
) -> requests.Response:
    """
    Make one HTTP request (no retries — wrap the caller in @retry).

    `timeout` is the read timeout; connecting is capped separately at
    CONNECT_TIMEOUT so an unreachable host fails fast even when the read
    timeout is long (local LLMs).
    """
    started = time.monotonic()
    try:
        response = (session or requests).request(
            method, url, timeout=(CONNECT_TIMEOUT, timeout), **kwargs)
    except requests.Timeout as exc:
        raise TransientError(f"{service} timed out ({type(exc).__name__}) after {timeout:.0f}s",
                             service=service) from exc
    except requests.ConnectionError as exc:
        raise TransientError(f"could not connect to {service}: {exc.__class__.__name__}",
                             service=service, hint=unreachable_hint) from exc

    log.debug("http", extra={"service": service, "method": method, "url": url,
                             "status": response.status_code,
                             "ms": round((time.monotonic() - started) * 1000)})

    if response.ok:
        return response
    if on_status is not None:
        specific = on_status(response)
        if specific is not None:
            raise specific
    raise classify(response, service)
