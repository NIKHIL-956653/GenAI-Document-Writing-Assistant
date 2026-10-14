"""
Shared HTTP retry/backoff for every outbound API call.

Jira, OpenRouter and Confluence all rate-limit (429) and all have transient
5xx windows. Without this, a single 429 kills a run that already cost money
in LLM tokens. Exponential backoff + jitter, and we honour Retry-After when
the server tells us how long to wait.
"""
import random
import time

import requests

# Status codes worth retrying: throttling and transient server-side failures.
# 4xx other than these are our bug — retrying just wastes time.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 1.0


def _backoff(attempt: int, base_delay: float) -> float:
    """Exponential backoff with jitter — jitter stops batch runs retrying in lockstep."""
    return base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)


def _delay_for(response, attempt: int, base_delay: float) -> float:
    """Honour Retry-After if present, otherwise back off exponentially."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            pass
    return _backoff(attempt, base_delay)


def request_with_retry(
    method: str,
    url: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    label: str = "request",
    **kwargs,
):
    """
    requests.request() with retries on throttling, transient 5xx, timeouts
    and connection errors. Returns the final Response (which may still be an
    error — callers keep their own status handling).
    """
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.request(method, url, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt == max_attempts:
                raise
            delay = _backoff(attempt, base_delay)
            print(f"  [retry] {label}: {type(exc).__name__} "
                  f"(attempt {attempt}/{max_attempts}) — waiting {delay:.1f}s")
            time.sleep(delay)
            continue

        if response.status_code in RETRYABLE_STATUS and attempt < max_attempts:
            delay = _delay_for(response, attempt, base_delay)
            print(f"  [retry] {label}: HTTP {response.status_code} "
                  f"(attempt {attempt}/{max_attempts}) — waiting {delay:.1f}s")
            time.sleep(delay)
            continue

        return response

    raise last_error  # pragma: no cover — loop always returns or raises first