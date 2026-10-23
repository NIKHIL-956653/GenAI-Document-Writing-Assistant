"""
One retry decorator for every outbound call.

Callers raise typed errors (see errors.py); this decorator decides what to
do with them:

- RateLimitError — the server asked us to slow down. Wait exactly as long as
  its Retry-After says (if it said), otherwise back off from a longer base
  than for transient failures, because throttling windows are measured in
  seconds-to-minutes. If the server asks for longer than `max_rate_limit_wait`,
  give up immediately rather than hang a batch run.
- TransientError — 5xx, timeouts, dropped connections. Exponential backoff
  with full jitter, so a batch of tickets that failed together does not
  retry in lockstep and hammer the service again.
- Anything else (auth, not-found, bad request, config) — re-raised at once;
  retrying a 401 only delays the error message.
"""
from __future__ import annotations

import functools
import logging
import random
import time
from typing import Callable, TypeVar

from docgen.errors import RateLimitError, RetryableError

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable)

# Indirection so tests can replace sleeping without monkeypatching `time`.
_sleep = time.sleep


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Full-jitter exponential backoff: uniform(0, min(cap, base * 2^(attempt-1)))."""
    return random.uniform(0, min(cap, base * (2 ** (attempt - 1))))


def retry(
    *,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    rate_limit_base_delay: float = 5.0,
    max_rate_limit_wait: float = 120.0,
) -> Callable[[F], F]:
    """
    Retry the wrapped function on RateLimitError and TransientError.

    `max_attempts` counts the first call, so 4 means up to 3 retries. When
    attempts run out the last error is re-raised unchanged, so callers and
    the CLI still see the specific failure.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 1
            while True:
                try:
                    return func(*args, **kwargs)
                except RetryableError as exc:
                    if attempt >= max_attempts:
                        log.error("giving up after retries", extra={
                            "service": exc.service, "call": func.__qualname__,
                            "attempts": attempt, "error": exc.message,
                        })
                        raise

                    if isinstance(exc, RateLimitError):
                        kind = "rate_limited"
                        if exc.retry_after is not None:
                            if exc.retry_after > max_rate_limit_wait:
                                log.error("rate limit wait exceeds budget", extra={
                                    "service": exc.service, "retry_after": exc.retry_after,
                                    "max_wait": max_rate_limit_wait,
                                })
                                raise
                            # Small jitter on top so parallel callers don't wake together.
                            delay = exc.retry_after + random.uniform(0, 1)
                        else:
                            delay = backoff_delay(attempt, rate_limit_base_delay,
                                                  max_rate_limit_wait)
                    else:
                        kind = "transient"
                        delay = backoff_delay(attempt, base_delay, max_delay)

                    log.warning("retrying", extra={
                        "service": exc.service, "call": func.__qualname__, "reason": kind,
                        "status": exc.status, "error": exc.message,
                        "attempt": attempt, "max_attempts": max_attempts,
                        "delay_s": round(delay, 2),
                    })
                    _sleep(delay)
                    attempt += 1

        return wrapper  # type: ignore[return-value]

    return decorator
