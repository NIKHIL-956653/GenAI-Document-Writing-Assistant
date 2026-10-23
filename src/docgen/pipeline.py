"""
Jira ticket(s) → LLM → Markdown file → (optional review) → publish, incrementally.

For each ticket:

1. fetch it, with as much context as `--context` asks for;
2. look up the document previously made for (ticket, template, target) in
   the store — if nothing the model would see has changed, skip it;
3. generate, resolve images, and diff against the stored version;
4. save the Markdown locally (always, before any publish can fail);
5. publish: update the existing Google Doc / Confluence page in place, or
   create one; record where it went.

Tickets run on a thread pool (`--concurrency`). A failure in one ticket is
recorded and the batch continues — except configuration and authentication
failures, which would fail identically for every ticket, so they cancel
the rest of the batch instead.
"""
from __future__ import annotations

import hashlib
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from functools import cached_property
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from docgen.assets import localize, resolve_images
from docgen.diff import colorize, summarize_changes
from docgen.errors import AuthError, ConfigError, DocgenError
from docgen.jira import JiraClient
from docgen.llm import Backend, generate, get_backend
from docgen.models import ContextLevel, ContextOptions, GeneratedDocument, RunResult, Ticket
from docgen.progress import Progress
from docgen.publishers import Publisher, get_publisher
from docgen.store import Store, StoredDocument
from docgen.templates import DEFAULT_TEMPLATE, Template, load_template

log = logging.getLogger(__name__)

# OpenRouter price per 1M tokens for the default model (input/output).
# Override with --price-in / --price-out when you switch models.
DEFAULT_PRICE_IN = 0.075
DEFAULT_PRICE_OUT = 0.30

# One lock per (ticket, template, target), process-wide: two jobs for the same ticket
# (a webhook retry, an API call racing the CLI) must not both create a document.
_key_locks: dict[tuple[str, str, str], threading.Lock] = {}
_key_locks_guard = threading.Lock()
_review_lock = threading.Lock()
_output_lock = threading.Lock()


class BatchAborted(DocgenError):
    """A running ticket stopped because another one hit a setup error."""


def _key_lock(key: tuple[str, str, str]) -> threading.Lock:
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


def output_filename(key: str, template: str) -> str:
    """DEMO-2_document.md for the default template, DEMO-2_runbook_document.md otherwise."""
    suffix = "" if template == DEFAULT_TEMPLATE else f"_{template}"
    return f"{key}{suffix}_document.md"


def confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        # Non-interactive (CI, piped input) — never publish without a human
        log.warning("no TTY for review prompt — skipping publish")
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class PipelineOptions(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    llm: str = "openrouter"
    model: str | None = None
    template: str = DEFAULT_TEMPLATE
    templates_dir: Path | None = None
    context: ContextLevel = ContextLevel.STANDARD
    publish: str = "none"
    public: bool = False
    review: bool = False
    force: bool = False           # regenerate even when the ticket hasn't changed
    show_diff: bool = False
    concurrency: int = Field(default=1, ge=1, le=32)
    price_in: float = DEFAULT_PRICE_IN
    price_out: float = DEFAULT_PRICE_OUT
    output_dir: Path = Path(".")
    interactive: bool = True      # False in the server: no browser consent, no prompts
    progress: bool | None = None  # None = draw a bar when stderr is a terminal


class Pipeline:
    def __init__(self, options: PipelineOptions | None = None, *, store: Store | None = None,
                 template: Template | None = None, **overrides):
        base = options or PipelineOptions()
        self.options = base.model_copy(update=overrides) if overrides else base
        self.store = store
        self.template = template or load_template(self.options.template, self.options.templates_dir)
        self.context = ContextOptions.for_level(self.options.context)
        self._abort = threading.Event()

    # Back-compat accessors used by callers and tests
    @property
    def publish(self) -> str:
        return self.options.publish

    @property
    def target(self) -> str:
        return "local" if self.options.publish == "none" else self.options.publish

    @cached_property
    def jira(self) -> JiraClient:
        cache = (self.store.path.parent if self.store else Path(".docgen")) / "attachments"
        return JiraClient(cache_dir=cache)

    @cached_property
    def backend(self) -> Backend:
        return get_backend(self.options.llm, self.options.model)

    @cached_property
    def publisher(self) -> Publisher:
        return get_publisher(self.options.publish, interactive=self.options.interactive)

    # ── batch ────────────────────────────────────────────────

    def run(self, tickets: list[str]) -> list[RunResult]:
        tickets = list(dict.fromkeys(tickets))
        if not tickets:
            return []
        # Build clients up front: lazy creation from several threads at once could, for
        # example, start two Google consent flows.
        _ = self.jira, self.backend
        if self.options.publish != "none":
            _ = self.publisher

        show_bar = len(tickets) > 1 and self.options.progress is not False
        progress = Progress(len(tickets), enabled=None if self.options.progress is None else True) \
            if show_bar else None
        results: dict[str, RunResult] = {}

        with progress or nullcontext():
            if self.options.concurrency == 1 or len(tickets) == 1:
                for key in tickets:
                    results[key] = self._tracked(key, progress)
            else:
                pool = ThreadPoolExecutor(max_workers=min(self.options.concurrency, len(tickets)),
                                          thread_name_prefix="docgen")
                futures = {pool.submit(self._tracked, key, progress): key for key in tickets}
                try:
                    for future in as_completed(futures):
                        results[futures[future]] = future.result()
                except KeyboardInterrupt:
                    self._abort.set()
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
                except BaseException:
                    # Config/auth failure: drop queued tickets, stop running ones at their next
                    # step, and wait for them so no thread outlives the run.
                    self._abort.set()
                    pool.shutdown(wait=True, cancel_futures=True)
                    raise
                pool.shutdown(wait=True)
        return [results[key] for key in tickets]

    def _tracked(self, key: str, progress: Progress | None) -> RunResult:
        if progress:
            progress.start(key)
        result = self.process_ticket(key)
        if progress:
            progress.finish(key, ok=result.ok, action=result.action)
        return result

    def process_ticket(self, key: str) -> RunResult:
        result = RunResult(ticket=key)
        started = time.monotonic()
        try:
            with _key_lock((key, self.template.name, self.target)):
                self._process(key, result)
            result.ok = True
        except (ConfigError, AuthError):
            raise
        except DocgenError as exc:
            result.error = exc.message
            log.error("ticket failed", extra={"ticket": key, "error": str(exc)})
        finally:
            result.seconds = time.monotonic() - started
        return result

    # ── one ticket ───────────────────────────────────────────

    def source_hash(self, ticket: Ticket) -> str:
        """Everything that determines the generated text: ticket + context, template, model."""
        template = self.template.model_dump_json(exclude={"source"})
        return _sha("\n".join([ticket.fingerprint(), _sha(template), self.backend.name,
                               self.backend.model, self.options.context.value]))

    def _checkpoint(self, key: str) -> None:
        if self._abort.is_set():
            raise BatchAborted(f"{key} cancelled: another ticket hit a configuration or "
                               "authentication error")

    def _process(self, key: str, result: RunResult) -> None:
        self._checkpoint(key)
        log.info("fetching ticket", extra={"ticket": key})
        ticket = self.jira.get_ticket(key, self.options.context)
        existing = self.store.get_document(ticket.key, self.template.name, self.target) \
            if self.store else None
        source_hash = self.source_hash(ticket)

        if existing and existing.source_hash == source_hash and not self.options.force:
            result.action, result.url = "unchanged", existing.url
            log.info("unchanged since last run — skipped",
                     extra={"ticket": key, "version": existing.version, "url": existing.url})
            return

        self._checkpoint(key)
        log.info("generating document", extra={"ticket": key, "backend": self.options.llm,
                                               "template": self.template.name,
                                               "context": self.options.context.value})
        document = generate(self.backend, ticket, self.template, context_chars=self.context.max_chars)
        body, images = resolve_images(document.body, ticket, self.template.output.heading_level)
        document = document.model_copy(update={"body": body, "images": images})
        result.chars = len(document.body)
        result.usage = document.usage
        result.cost = document.usage.cost(self.options.price_in, self.options.price_out)

        markdown = document.to_markdown()
        if existing:
            result.changes = summarize_changes(existing.markdown, markdown, existing.version)
            log.info("document changed" if result.changes.changed else "regenerated, no changes",
                     extra={"ticket": key, "from_version": existing.version,
                            "changes": result.changes.describe()})
            if self.options.show_diff and result.changes.changed:
                self._print(f"\n{key}: {result.changes.describe()}\n{self._diff_text(result.changes.diff)}\n")

        path = self._save_local(document)
        if self.options.publish == "none":
            result.action, result.url = "saved", str(path)
            self._remember(ticket, document, markdown, source_hash, existing,
                           external_id=str(path), url=str(path))
            return

        if existing and result.changes and not result.changes.changed and not self.options.force:
            result.action, result.url = "unchanged", existing.url
            self._remember(ticket, document, markdown, source_hash, existing,
                           external_id=existing.external_id, url=existing.url, bump=False)
            return

        if self.options.review and not self._approved(document):
            result.action = "declined"
            log.info("not published — review declined", extra={"ticket": key})
            return

        self._checkpoint(key)
        log.info("publishing", extra={"ticket": key, "target": self.options.publish,
                                      "mode": "update" if existing else "create"})
        published = self.publisher.publish(document, public=self.options.public,
                                           existing_id=existing.external_id if existing else None)
        result.action = "created" if published.created else "updated"
        result.url = published.url
        self._remember(ticket, document, markdown, source_hash, existing,
                       external_id=published.external_id, url=published.url,
                       restart=published.created)

    def _save_local(self, document: GeneratedDocument) -> Path:
        out = self.options.output_dir
        path = out / output_filename(document.ticket_key, self.template.name)
        markdown = localize(document.to_markdown(), document.images,
                            out / f"{document.ticket_key}_attachments", relative_to=out)
        path.write_text(markdown, encoding="utf-8")
        log.info("saved", extra={"ticket": document.ticket_key, "path": str(path),
                                 "chars": len(document.body), "images": len(document.images),
                                 "tokens": document.usage.total_tokens,
                                 "cost_usd": document.usage.cost(self.options.price_in,
                                                                 self.options.price_out)})
        return path

    def _remember(self, ticket: Ticket, document: GeneratedDocument, markdown: str, source_hash: str,
                  existing: StoredDocument | None, *, external_id: str, url: str,
                  bump: bool = True, restart: bool = False) -> None:
        if self.store is None:
            return
        if existing and not restart:
            version = existing.version + (1 if bump else 0)
            created_at = existing.created_at
        else:
            version, created_at = 1, None
        record = StoredDocument(
            ticket_key=ticket.key, template=self.template.name, target=self.target,
            external_id=external_id, url=url, title=document.title, source_hash=source_hash,
            content_hash=_sha(markdown), markdown=markdown, version=version,
            **({"created_at": created_at} if created_at else {}))
        self.store.save_document(record)

    def _approved(self, document: GeneratedDocument) -> bool:
        with _review_lock:  # one prompt at a time, however many tickets run in parallel
            self._print(f"\n{'-' * 60}\n{document.body}\n{'-' * 60}\n")
            return confirm(f"Publish {document.ticket_key} to {self.options.publish}?")

    @staticmethod
    def _diff_text(diff: str) -> str:
        return colorize(diff) if sys.stdout.isatty() else diff

    @staticmethod
    def _print(text: str) -> None:
        from docgen import progress
        bar = progress.active()
        with _output_lock, (bar._lock if bar else nullcontext()):
            if bar:
                bar.clear()
            sys.stdout.write(text)
            sys.stdout.flush()
            if bar:
                bar.redraw()


def log_summary(results: list[RunResult]) -> None:
    """The numbers that go in the README and the interview reference."""
    for r in results:
        log.info("result", extra={
            "ticket": r.ticket, "status": "ok" if r.ok else "failed",
            **({"action": r.action} if r.action else {}),
            "seconds": round(r.seconds, 1),
            **({"tokens": r.usage.total_tokens} if r.usage and r.usage.total_tokens else {}),
            **({"cost_usd": round(r.cost, 6)} if r.cost is not None else {}),
            **({"changes": r.changes.describe()} if r.changes else {}),
            **({"url": r.url} if r.url else {}),
            **({"error": r.error} if r.error else {}),
        })

    ok = [r for r in results if r.ok]
    summary: dict = {"succeeded": len(ok), "failed": len(results) - len(ok)}
    # "created" is a reserved LogRecord attribute, hence "new"
    for action, label in (("created", "new"), ("updated", "updated"), ("saved", "saved"),
                          ("unchanged", "unchanged"), ("declined", "declined")):
        count = sum(1 for r in ok if r.action == action)
        if count:
            summary[label] = count
    generated = [r for r in ok if r.usage is not None]
    if generated:
        total = sum(r.seconds for r in generated)
        summary.update(total_seconds=round(total, 1), avg_seconds=round(total / len(generated), 1))
        tokens = [r.usage.total_tokens for r in generated if r.usage and r.usage.total_tokens]
        if tokens:
            summary["avg_tokens"] = sum(tokens) // len(tokens)
        costs = [r.cost for r in generated if r.cost is not None]
        if costs:
            summary.update(total_cost_usd=round(sum(costs), 6),
                           avg_cost_usd=round(sum(costs) / len(costs), 6))
    log.info("run summary", extra=summary)
