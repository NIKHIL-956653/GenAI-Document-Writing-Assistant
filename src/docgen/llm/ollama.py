"""Local Ollama backend — free, private, same prompt. Slow on CPU."""
from __future__ import annotations

import requests

from docgen import http
from docgen.config import OllamaSettings, load
from docgen.errors import APIError, DocgenError, TransientError
from docgen.llm.base import Completion
from docgen.models import TokenUsage
from docgen.retry import retry

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

    @property
    def model(self) -> str:
        return self.settings.model

    # Fewer attempts than cloud APIs: a local server that is down stays down.
    @retry(max_attempts=3)
    def complete(self, messages: list[dict]) -> Completion:
        model = self.settings.model
        response = http.send(
            "POST", f"{self.settings.host.rstrip('/')}/api/chat",
            service=SERVICE, timeout=self.settings.timeout,
            json={"model": model, "messages": messages, "stream": False},
            on_status=self._status_error,
            unreachable_hint=f"Is Ollama running at {self.settings.host}? Start it with "
                             "`ollama serve`, or set OLLAMA_HOST",
        )
        try:
            data = response.json()
            text = data["message"]["content"]
        except (ValueError, KeyError, TypeError) as exc:
            raise TransientError("ollama response had no message", service=SERVICE) from exc
        if not text or not text.strip():
            raise TransientError("ollama returned an empty completion", service=SERVICE)

        prompt_tokens = data.get("prompt_eval_count")
        completion_tokens = data.get("eval_count")
        usage = TokenUsage(
            model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=(prompt_tokens or 0) + (completion_tokens or 0) or None,
            cost_usd=0.0,  # runs locally
        )
        return Completion(text=text, usage=usage)
