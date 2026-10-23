"""Local Ollama backend — free, private, same prompt. Slow on CPU."""
from __future__ import annotations

import logging

import requests

from docgen import http
from docgen.config import OllamaSettings, load
from docgen.errors import APIError, DocgenError, TransientError
from docgen.llm.prompt import build_messages
from docgen.models import GeneratedDocument, Ticket, TokenUsage
from docgen.retry import retry

log = logging.getLogger(__name__)

SERVICE = "ollama"


class OllamaBackend:
    name = "ollama"

    def __init__(self, settings: OllamaSettings | None = None):
        self.settings = settings or load(OllamaSettings)

    def _status_error(self, response: requests.Response) -> DocgenError | None:
        if response.status_code == 404:
            model = self.settings.model
            return APIError(f"Ollama model '{model}' is not available locally (HTTP 404)",
                            service=SERVICE, status=404,
                            hint=f"Run `ollama pull {model}`, or set OLLAMA_MODEL to a pulled "
                                 "model (`ollama list`)")
        return None

    # Fewer attempts than cloud APIs: a local server that is down stays down.
    @retry(max_attempts=3)
    def generate(self, ticket: Ticket) -> GeneratedDocument:
        model = self.settings.model
        response = http.send(
            "POST", f"{self.settings.host.rstrip('/')}/api/chat",
            service=SERVICE, timeout=self.settings.timeout,
            json={"model": model, "messages": build_messages(ticket), "stream": False},
            on_status=self._status_error,
            unreachable_hint=f"Is Ollama running at {self.settings.host}? Start it with "
                             "`ollama serve`, or set OLLAMA_HOST",
        )
        try:
            data = response.json()
            body = data["message"]["content"]
        except (ValueError, KeyError, TypeError) as exc:
            raise TransientError("ollama response had no message", service=SERVICE) from exc
        if not body or not body.strip():
            raise TransientError("ollama returned an empty completion", service=SERVICE)

        prompt_tokens = data.get("prompt_eval_count")
        completion_tokens = data.get("eval_count")
        usage = TokenUsage(
            model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=(prompt_tokens or 0) + (completion_tokens or 0) or None,
        )
        log.info("document generated", extra={"ticket": ticket.key, "backend": self.name,
                                              "model": model, "chars": len(body),
                                              "tokens": usage.total_tokens})
        return GeneratedDocument(ticket_key=ticket.key, title=ticket.title, body=body,
                                 backend=self.name, usage=usage)
