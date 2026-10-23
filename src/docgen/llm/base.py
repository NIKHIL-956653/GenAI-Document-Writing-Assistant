"""The contract every LLM backend implements."""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from docgen.models import TokenUsage


class Completion(BaseModel):
    text: str
    usage: TokenUsage


class Backend(Protocol):
    name: str

    @property
    def model(self) -> str: ...

    def complete(self, messages: list[dict]) -> Completion: ...
