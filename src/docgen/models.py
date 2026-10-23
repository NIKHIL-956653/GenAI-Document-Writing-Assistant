"""Data structures that flow through the pipeline: Ticket → GeneratedDocument → RunResult."""
from __future__ import annotations

import hashlib
import re
from enum import Enum

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


class ContextLevel(str, Enum):
    """How much of the ticket's surroundings to pull into the prompt (`--context`)."""
    MINIMAL = "minimal"     # title, description, status, labels
    STANDARD = "standard"   # + recent comments, linked issues, subtasks, attachment names
    FULL = "full"           # + all comments, linked/subtask descriptions, images downloaded and embedded


class ContextOptions(BaseModel):
    max_comments: int
    links: bool
    subtasks: bool
    related_descriptions: bool
    attachments: bool          # list attachment names in the prompt
    download_images: bool      # download images and embed them in the published document
    max_related: int = 10
    max_images: int = 10
    max_image_bytes: int = 10 * 1024 * 1024
    max_chars: int = 15_000   # cap on the context block sent to the model

    @classmethod
    def for_level(cls, level: ContextLevel | str) -> ContextOptions:
        level = ContextLevel(level)
        if level is ContextLevel.MINIMAL:
            return cls(max_comments=0, links=False, subtasks=False, related_descriptions=False,
                       attachments=False, download_images=False)
        if level is ContextLevel.STANDARD:
            return cls(max_comments=10, links=True, subtasks=True, related_descriptions=False,
                       attachments=True, download_images=False)
        return cls(max_comments=50, links=True, subtasks=True, related_descriptions=True,
                   attachments=True, download_images=True, max_chars=40_000)


class Comment(BaseModel):
    author: str = "unknown"
    created: str = ""
    body: str


class RelatedIssue(BaseModel):
    """A linked issue or a subtask."""
    key: str
    relation: str = "subtask"       # e.g. "blocks", "is blocked by", "relates to", "subtask"
    summary: str = ""
    status: str = ""
    description: str | None = None


IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif"}   # what Google Docs can embed


class Attachment(BaseModel):
    id: str
    filename: str
    mime_type: str = "application/octet-stream"
    size: int = 0
    content_url: str = ""
    # Set once downloaded; excluded from fingerprints so the cache path never forces regeneration.
    local_path: str | None = None

    @property
    def is_image(self) -> bool:
        return self.mime_type.lower() in IMAGE_TYPES


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …[truncated]"


class Ticket(BaseModel):
    key: str
    title: str
    description: str = "No description"
    status: str = "Unknown"
    labels: list[str] = Field(default_factory=list)
    issue_type: str | None = None
    priority: str | None = None
    comments: list[Comment] = Field(default_factory=list)
    links: list[RelatedIssue] = Field(default_factory=list)
    subtasks: list[RelatedIssue] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def _valid_key(cls, value: str) -> str:
        return normalise_ticket_key(value)

    @field_validator("labels", mode="before")
    @classmethod
    def _none_is_empty(cls, value):
        return value or []

    @property
    def images(self) -> list[Attachment]:
        return [a for a in self.attachments if a.is_image and a.local_path]

    def context_text(self, max_chars: int = 15_000) -> str:
        """
        Comments, linked issues, subtasks and attachments as a prompt block.
        Over `max_chars`, the oldest comments are dropped first, then the block is cut.
        """
        def related(title: str, items: list[RelatedIssue]) -> list[str]:
            if not items:
                return []
            lines = [title]
            for item in items:
                status = f" ({item.status})" if item.status else ""
                prefix = f"{item.relation} " if item.relation != "subtask" else ""
                lines.append(f"- {prefix}{item.key}: {item.summary}{status}")
                if item.description:
                    lines.append("  " + _clip(item.description, 1500).replace("\n", "\n  "))
            return lines

        head = []
        if self.issue_type or self.priority:
            head.append(" · ".join(filter(None, [
                f"ISSUE TYPE: {self.issue_type}" if self.issue_type else "",
                f"PRIORITY: {self.priority}" if self.priority else ""])))
        tail = related("LINKED ISSUES:", self.links) + related("SUBTASKS:", self.subtasks)
        if self.attachments:
            tail.append("ATTACHMENTS:")
            for a in self.attachments:
                if a in self.images:
                    from docgen.assets import image_name  # local import: assets imports models
                    tail.append(f"- {image_name(a)} (image, can be embedded)")
                else:
                    tail.append(f"- {a.filename}")

        comments = [f"- [{c.created[:10]}] {c.author}: {_clip(c.body, 1500)}" for c in self.comments]
        omitted = 0
        while True:
            block = list(head)
            if comments:
                note = f", {omitted} older omitted" if omitted else ""
                block += [f"COMMENTS ({len(comments)}{note}, oldest first):", *comments]
            text = "\n".join(block + tail)
            if len(text) <= max_chars or not comments:
                return _clip(text, max_chars) if text else ""
            comments.pop(0)
            omitted += 1

    def full_text(self) -> str:
        """Everything the model was shown — the reference text for faithfulness checks."""
        return "\n".join([self.key, self.title, self.description, self.status, *self.labels,
                          self.context_text(max_chars=10**9)])

    def fingerprint(self) -> str:
        """Changes whenever anything the model sees changes — drives incremental skips."""
        data = self.model_dump_json(exclude={"attachments": {"__all__": {"local_path"}}})
        return hashlib.sha256(data.encode()).hexdigest()


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
    # Images the body references as ![caption](attachment:FILENAME)
    images: list[Attachment] = Field(default_factory=list)

    def to_markdown(self) -> str:
        return f"# {self.title}\n\n{self.body}"


class Published(BaseModel):
    """Where a publisher put a document."""
    target: str
    external_id: str
    url: str
    created: bool = True     # False when an existing document was updated in place


class ChangeSummary(BaseModel):
    """What changed since the previously stored version of a document."""
    previous_version: int
    added_sections: list[str] = Field(default_factory=list)
    removed_sections: list[str] = Field(default_factory=list)
    changed_sections: list[str] = Field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0
    diff: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.lines_added or self.lines_removed)

    def describe(self) -> str:
        if not self.changed:
            return "no changes"
        parts = [f"+{self.lines_added}/-{self.lines_removed} lines"]
        for label, items in (("added", self.added_sections), ("removed", self.removed_sections),
                             ("changed", self.changed_sections)):
            if items:
                parts.append(f"{label}: {', '.join(items)}")
        return "; ".join(parts)


class RunResult(BaseModel):
    """One row of the run summary."""
    ticket: str
    ok: bool = False
    # created / updated (published), saved (local only), unchanged (skipped), declined (review)
    action: str = ""
    chars: int = 0
    seconds: float = 0.0
    usage: TokenUsage | None = None
    cost: float | None = None
    url: str = ""
    changes: ChangeSummary | None = None
    error: str | None = None
