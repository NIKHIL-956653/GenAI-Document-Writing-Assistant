"""Data structures that flow through the pipeline: Ticket → GeneratedDocument → RunResult."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from docgen.errors import InvalidTicketKeyError

# Jira project keys start with a letter and contain letters, digits or
# underscores; the issue number follows a single hyphen. e.g. DEMO-2, AB_C-104
TICKET_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-[1-9]\d*$")


def normalise_ticket_key(raw: str) -> str:
    """Upper-case and validate a ticket key, or raise InvalidTicketKeyError."""
    key = raw.strip().upper()
    if not TICKET_KEY_RE.match(key):
        raise InvalidTicketKeyError(
            f"'{raw}' is not a valid Jira issue key",
            hint="Use the PROJECT-NUMBER form shown in Jira, e.g. DEMO-2",
        )
    return key


class Ticket(BaseModel):
    key: str
    title: str
    description: str = "No description"
    status: str = "Unknown"
    labels: list[str] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def _valid_key(cls, value: str) -> str:
        return normalise_ticket_key(value)

    @field_validator("labels", mode="before")
    @classmethod
    def _none_is_empty(cls, value):
        return value or []


class TokenUsage(BaseModel):
    """Token counts as reported by the backend; None where it reported nothing."""
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    # Billed cost when the provider reports it (OpenRouter), 0.0 for local models.
    cost_usd: float | None = None

    def cost(self, price_in: float, price_out: float) -> float | None:
        """
        USD for this call: the provider-reported cost when there is one, otherwise an
        estimate from per-1M-token prices, or None if there are no token counts either.
        """
        if self.cost_usd is not None:
            return self.cost_usd
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return (self.prompt_tokens / 1_000_000 * price_in
                + self.completion_tokens / 1_000_000 * price_out)


class GeneratedDocument(BaseModel):
    ticket_key: str
    title: str
    body: str = Field(description="Markdown produced by the LLM")
    template: str = "technical"
    backend: str
    usage: TokenUsage

    def to_markdown(self) -> str:
        return f"# {self.title}\n\n{self.body}"


class RunResult(BaseModel):
    """One row of the run summary."""
    ticket: str
    ok: bool = False
    chars: int = 0
    seconds: float = 0.0
    usage: TokenUsage | None = None
    cost: float | None = None
    url: str = ""
    error: str | None = None
