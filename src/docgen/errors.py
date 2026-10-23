"""
Exception hierarchy.

Every failure the pipeline can hit maps to one of these, so the CLI can print
a one-line, actionable message instead of a traceback, and the retry
decorator can tell "wait and try again" apart from "this will never work".
"""
from __future__ import annotations


class DocgenError(Exception):
    """Base class. `hint` is the "what to do about it" line shown to the user."""

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message}\n  → {self.hint}" if self.hint else self.message


# ── Setup failures: fail fast, never retry ───────────────────

class ConfigError(DocgenError):
    """Missing or invalid configuration (env vars, credential files)."""


class AuthError(DocgenError):
    """Credentials were rejected or have expired."""


class InvalidTicketKeyError(DocgenError):
    """The ticket key is malformed (caught before any network call)."""


class TicketNotFoundError(DocgenError):
    """Jira says the ticket does not exist, or the user cannot see it."""


# ── API failures ─────────────────────────────────────────────

class APIError(DocgenError):
    """A remote call failed. `service` names which one (jira, openrouter, ...)."""

    def __init__(self, message: str, *, service: str, status: int | None = None,
                 hint: str | None = None):
        super().__init__(message, hint=hint)
        self.service = service
        self.status = status


class RetryableError(APIError):
    """Base for failures worth retrying."""


class RateLimitError(RetryableError):
    """
    The server throttled us (429, or Google's 403 rateLimitExceeded).
    `retry_after` is the server's requested wait in seconds, if it gave one.
    """

    def __init__(self, message: str, *, service: str, status: int | None = None,
                 retry_after: float | None = None, hint: str | None = None):
        super().__init__(message, service=service, status=status, hint=hint)
        self.retry_after = retry_after


class TransientError(RetryableError):
    """5xx, timeouts, dropped connections — likely to succeed on a later attempt."""
