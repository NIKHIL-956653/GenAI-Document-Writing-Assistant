"""
Jira ticket → LLM → Markdown file → (optional review) → publish.

Batch-capable. Clients are built once and reused across tickets. A failure
in one ticket is recorded and the batch continues — except configuration and
authentication failures, which would fail identically for every ticket, so
they abort the run instead of repeating the same error N times.
"""
from __future__ import annotations

import logging
import sys
import time
from functools import cached_property
from pathlib import Path

from docgen.errors import AuthError, ConfigError, DocgenError
from docgen.jira import JiraClient
from docgen.llm import Backend, get_backend
from docgen.models import RunResult
from docgen.publishers import Publisher, get_publisher

log = logging.getLogger(__name__)

# OpenRouter price per 1M tokens for the default model (input/output).
# Override with --price-in / --price-out when you switch models.
DEFAULT_PRICE_IN = 0.075
DEFAULT_PRICE_OUT = 0.30


def confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        # Non-interactive (CI, piped input) — never publish without a human
        log.warning("no TTY for review prompt — skipping publish")
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


class Pipeline:
    def __init__(
        self,
        llm: str = "openrouter",
        publish: str = "none",
        public: bool = False,
        review: bool = False,
        price_in: float = DEFAULT_PRICE_IN,
        price_out: float = DEFAULT_PRICE_OUT,
        output_dir: Path = Path("."),
    ):
        self.llm = llm
        self.publish = publish
        self.public = public
        self.review = review
        self.price_in = price_in
        self.price_out = price_out
        self.output_dir = output_dir

    @cached_property
    def jira(self) -> JiraClient:
        return JiraClient()

    @cached_property
    def backend(self) -> Backend:
        return get_backend(self.llm)

    @cached_property
    def publisher(self) -> Publisher:
        return get_publisher(self.publish)

    def run(self, tickets: list[str]) -> list[RunResult]:
        results = []
        for key in tickets:
            results.append(self.process_ticket(key))
        return results

    def process_ticket(self, key: str) -> RunResult:
        result = RunResult(ticket=key)
        started = time.monotonic()
        try:
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

    def _process(self, key: str, result: RunResult) -> None:
        log.info("fetching ticket", extra={"ticket": key})
        ticket = self.jira.get_ticket(key)

        log.info("generating document", extra={"ticket": key, "backend": self.llm})
        document = self.backend.generate(ticket)
        result.chars = len(document.body)
        result.usage = document.usage
        result.cost = document.usage.cost(self.price_in, self.price_out)

        # Save locally before any publish can fail, so a generated doc is never lost.
        path = self.output_dir / f"{ticket.key}_document.md"
        path.write_text(document.to_markdown(), encoding="utf-8")
        log.info("saved", extra={"ticket": key, "path": str(path), "chars": result.chars,
                                 "tokens": document.usage.total_tokens,
                                 "cost_usd": result.cost})

        if self.publish == "none":
            log.info("publish skipped", extra={"ticket": key})
            return

        if self.review:
            sys.stdout.write(f"\n{'-' * 60}\n{document.body}\n{'-' * 60}\n")
            sys.stdout.flush()
            if not confirm(f"Publish {ticket.key} to {self.publish}?"):
                log.info("not published — review declined", extra={"ticket": key})
                return

        log.info("publishing", extra={"ticket": key, "target": self.publish})
        result.url = self.publisher.publish(document, public=self.public)


def log_summary(results: list[RunResult]) -> None:
    """The numbers that go in the README and the interview reference."""
    for r in results:
        log.info("result", extra={
            "ticket": r.ticket, "status": "ok" if r.ok else "failed",
            "seconds": round(r.seconds, 1),
            **({"tokens": r.usage.total_tokens} if r.usage and r.usage.total_tokens else {}),
            **({"cost_usd": round(r.cost, 6)} if r.cost is not None else {}),
            **({"url": r.url} if r.url else {}),
            **({"error": r.error} if r.error else {}),
        })

    ok = [r for r in results if r.ok]
    summary: dict = {"succeeded": len(ok), "failed": len(results) - len(ok)}
    if ok:
        total = sum(r.seconds for r in ok)
        summary.update(total_seconds=round(total, 1), avg_seconds=round(total / len(ok), 1))
        tokens = [r.usage.total_tokens for r in ok if r.usage and r.usage.total_tokens]
        if tokens:
            summary["avg_tokens"] = sum(tokens) // len(tokens)
        costs = [r.cost for r in ok if r.cost is not None]
        if costs:
            summary.update(total_cost_usd=round(sum(costs), 6),
                           avg_cost_usd=round(sum(costs) / len(costs), 6))
    log.info("run summary", extra=summary)
