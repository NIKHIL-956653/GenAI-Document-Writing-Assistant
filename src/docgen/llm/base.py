"""The contract every LLM backend implements."""
from __future__ import annotations

from typing import Protocol

from docgen.models import GeneratedDocument, Ticket


class Backend(Protocol):
    name: str

    def generate(self, ticket: Ticket) -> GeneratedDocument: ...
