"""OpenRouter (OpenAI-compatible chat completions) backend."""
from __future__ import annotations

import logging

import requests

from docgen import http
from docgen.config import OpenRouterSettings, load
from docgen.errors import (APIError, AuthError, DocgenError, RateLimitError,
                           TransientError)
from docgen.llm.prompt import build_messages
from docgen.models import GeneratedDocument, Ticket, TokenUsage
from docgen.retry import retry

log = logging.getLogger(__name__)

SERVICE = "openrouter"


def _status_error(response: requests.Response) -> DocgenError | None:
    status = response.status_code
    if status == 401:
        return AuthError("OpenRouter rejected the API key (HTTP 401)",
                         hint="Check OPENROUTER_API_KEY — keys are at https://openrouter.ai/keys")
    if status == 402:
        return APIError("OpenRouter account has insufficient credits (HTTP 402)",
                        service=SERVICE, status=status,
                        hint="Add credits at https://openrouter.ai/credits or pick a free model "
                             "with OPENROUTER_MODEL")
    return None


def _error_in_body(data: dict) -> DocgenError | None:
    """OpenRouter can return HTTP 200 with an `error` object when the upstream provider fails."""
    error = data.get("error")
    if not error:
        return None
    code = error.get("code") if isinstance(error, dict) else None
    message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
    detail = f"openrouter upstream error {code}: {message}"
    if code == 429:
        return RateLimitError(detail, service=SERVICE, status=code)
    if code is None or code == 408 or (isinstance(code, int) and code >= 500):
        return TransientError(detail, service=SERVICE, status=code)
    return APIError(detail, service=SERVICE, status=code)


class OpenRouterBackend:
    name = "openrouter"

    def __init__(self, settings: OpenRouterSettings | None = None):
        self.settings = settings or load(OpenRouterSettings)

    @retry()
    def generate(self, ticket: Ticket) -> GeneratedDocument:
        model = self.settings.model
        response = http.send(
            "POST", f"{self.settings.base_url}/chat/completions",
            service=SERVICE, timeout=self.settings.timeout,
            headers={"Authorization": f"Bearer {self.settings.api_key.get_secret_value()}",
                     "Content-Type": "application/json"},
            json={"model": model, "messages": build_messages(ticket)},
            on_status=_status_error,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise TransientError("openrouter returned a non-JSON body", service=SERVICE) from exc

        if (err := _error_in_body(data)) is not None:
            raise err

        try:
            body = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TransientError("openrouter response had no completion",
                                 service=SERVICE) from exc
        if not body or not body.strip():
            raise TransientError("openrouter returned an empty completion", service=SERVICE)

        raw = data.get("usage") or {}
        usage = TokenUsage(model=model,
                           prompt_tokens=raw.get("prompt_tokens"),
                           completion_tokens=raw.get("completion_tokens"),
                           total_tokens=raw.get("total_tokens"))
        log.info("document generated", extra={"ticket": ticket.key, "backend": self.name,
                                              "model": model, "chars": len(body),
                                              "tokens": usage.total_tokens})
        return GeneratedDocument(ticket_key=ticket.key, title=ticket.title, body=body,
                                 backend=self.name, usage=usage)
