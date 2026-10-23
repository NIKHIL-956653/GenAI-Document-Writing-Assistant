import pytest

from docgen.errors import (APIError, AuthError, RateLimitError, TransientError)
from docgen.http import parse_retry_after
from docgen.retry import backoff_delay, retry


def flaky(errors, result="done"):
    calls = {"n": 0}

    def func():
        calls["n"] += 1
        if errors:
            raise errors.pop(0)
        return result

    return func, calls


def test_returns_immediately_on_success(sleeps):
    func, calls = flaky([])
    assert retry()(func)() == "done"
    assert calls["n"] == 1 and sleeps == []


def test_transient_errors_are_retried_then_succeed(sleeps):
    func, calls = flaky([TransientError("502", service="x"), TransientError("t/o", service="x")])
    assert retry(max_attempts=4)(func)() == "done"
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_gives_up_after_max_attempts_and_reraises_last_error(sleeps):
    errors = [TransientError(f"fail {i}", service="x") for i in range(5)]
    func, calls = flaky(errors)
    with pytest.raises(TransientError, match="fail 2"):
        retry(max_attempts=3)(func)()
    assert calls["n"] == 3
    assert len(sleeps) == 2


@pytest.mark.parametrize("error", [
    AuthError("401"),
    APIError("400", service="x", status=400),
    ValueError("bug"),
])
def test_non_retryable_errors_raise_immediately(sleeps, error):
    func, calls = flaky([error])
    with pytest.raises(type(error)):
        retry()(func)()
    assert calls["n"] == 1 and sleeps == []


def test_rate_limit_honours_retry_after(sleeps):
    func, _ = flaky([RateLimitError("429", service="x", retry_after=7)])
    retry()(func)()
    assert 7 <= sleeps[0] <= 8  # Retry-After plus at most 1s jitter


def test_rate_limit_without_retry_after_uses_longer_base(sleeps, monkeypatch):
    monkeypatch.setattr("random.uniform", lambda lo, hi: hi)  # take the ceiling
    func, _ = flaky([RateLimitError("429", service="x"), TransientError("503", service="x")])
    retry(base_delay=1, rate_limit_base_delay=5)(func)()
    # attempt 1 rate-limited: 5 * 2^0; attempt 2 transient: 1 * 2^1
    assert sleeps == [5, 2]


def test_rate_limit_wait_beyond_budget_fails_fast(sleeps):
    func, calls = flaky([RateLimitError("429", service="x", retry_after=3600)])
    with pytest.raises(RateLimitError):
        retry(max_rate_limit_wait=120)(func)()
    assert calls["n"] == 1 and sleeps == []


def test_backoff_is_capped_and_jittered():
    for attempt in range(1, 12):
        delay = backoff_delay(attempt, base=1, cap=30)
        assert 0 <= delay <= min(30, 2 ** (attempt - 1))


def test_preserves_function_metadata():
    @retry()
    def documented():
        """doc"""
    assert documented.__name__ == "documented"
    assert documented.__doc__ == "doc"


@pytest.mark.parametrize("value, expected", [
    (None, None), ("", None), ("12", 12.0), ("1.5", 1.5), ("garbage", None),
    ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),  # a date in the past → no wait
])
def test_parse_retry_after(value, expected):
    assert parse_retry_after(value) == expected
