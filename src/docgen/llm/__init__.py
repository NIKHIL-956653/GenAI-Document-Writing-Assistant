"""LLM backends. Same prompt, swappable transport."""
from __future__ import annotations

from docgen.llm.base import Backend
from docgen.llm.prompt import SYSTEM_PROMPT, build_messages

BACKENDS = ("openrouter", "ollama")


def get_backend(name: str) -> Backend:
    if name == "openrouter":
        from docgen.llm.openrouter import OpenRouterBackend
        return OpenRouterBackend()
    if name == "ollama":
        from docgen.llm.ollama import OllamaBackend
        return OllamaBackend()
    raise ValueError(f"unknown LLM backend {name!r}; choose from {', '.join(BACKENDS)}")


__all__ = ["BACKENDS", "Backend", "SYSTEM_PROMPT", "build_messages", "get_backend"]
