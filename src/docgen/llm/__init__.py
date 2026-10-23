"""LLM backends. Same prompt, swappable transport."""
from __future__ import annotations

import logging

from docgen.config import OllamaSettings, OpenRouterSettings, load
from docgen.llm.base import Backend, Completion
from docgen.llm.prompt import build_messages, system_prompt
from docgen.models import GeneratedDocument, Ticket
from docgen.templates import Template

log = logging.getLogger(__name__)

BACKENDS = ("openrouter", "ollama")


def get_backend(name: str, model: str | None = None) -> Backend:
    """Build a backend; `model` overrides the configured default model."""
    update = {"model": model} if model else {}
    if name == "openrouter":
        from docgen.llm.openrouter import OpenRouterBackend
        return OpenRouterBackend(load(OpenRouterSettings).model_copy(update=update))
    if name == "ollama":
        from docgen.llm.ollama import OllamaBackend
        return OllamaBackend(load(OllamaSettings).model_copy(update=update))
    raise ValueError(f"unknown LLM backend {name!r}; choose from {', '.join(BACKENDS)}")


def generate(backend: Backend, ticket: Ticket, template: Template, *,
             context_chars: int = 15_000) -> GeneratedDocument:
    completion = backend.complete(build_messages(ticket, template, context_chars=context_chars))
    log.info("document generated", extra={
        "ticket": ticket.key, "template": template.name, "backend": backend.name,
        "model": backend.model, "chars": len(completion.text),
        "tokens": completion.usage.total_tokens,
    })
    return GeneratedDocument(ticket_key=ticket.key, title=ticket.title, body=completion.text,
                             template=template.name, backend=backend.name,
                             usage=completion.usage)


__all__ = ["BACKENDS", "Backend", "Completion", "build_messages", "generate", "get_backend",
           "system_prompt"]
