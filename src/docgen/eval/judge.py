"""
LLM-as-judge faithfulness: a second model lists the document's claims and
marks each as supported by the ticket or not.

Stronger than the entity-grounding heuristic (it reads meaning, not tokens)
but it costs tokens and is itself a model, so it is opt-in (`--judge`) and
its own token usage and cost are reported separately from generation.
"""
from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field, ValidationError

from docgen.errors import DocgenError
from docgen.eval.scorers import Score
from docgen.llm.base import Backend
from docgen.models import Ticket, TokenUsage

log = logging.getLogger(__name__)

JUDGE_SYSTEM = """You are a strict fact-checker for technical documentation.
You compare a generated document against the source ticket it was written from.
A claim is SUPPORTED if the ticket states it or it follows directly from the ticket.
A claim is also SUPPORTED if it is generic standard engineering practice that asserts
nothing specific about this system (e.g. "write unit tests", "use HTTPS").
A claim is UNSUPPORTED if it adds specifics the ticket does not give: numbers, limits,
technologies, endpoints, names, dates, causes, or behaviour.
Statements that something is unknown or unspecified are not claims; skip them.
Respond with JSON only."""

JUDGE_USER = """SOURCE TICKET
{ticket}

GENERATED DOCUMENT
{document}

List every factual claim in the generated document and judge it. Respond with exactly
this JSON shape and nothing else:
{{"claims": [{{"claim": "<short paraphrase>", "supported": true, "reason": "<why>"}}]}}"""


class JudgedClaim(BaseModel):
    claim: str
    supported: bool
    reason: str = ""


class Verdict(BaseModel):
    claims: list[JudgedClaim] = Field(default_factory=list)


def parse_verdict(text: str) -> Verdict:
    """Accept bare JSON, fenced JSON, or JSON embedded in chatter."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidates.append(cleaned[start:end + 1])
    for candidate in candidates:
        try:
            return Verdict.model_validate(json.loads(candidate))
        except (ValueError, ValidationError):
            continue
    raise ValueError("judge did not return the expected JSON")


def judge_faithfulness(judge: Backend, document: str, ticket: Ticket) -> tuple[Score, TokenUsage | None]:
    ticket_text = (f"ID: {ticket.key}\nTITLE: {ticket.title}\nSTATUS: {ticket.status}\n"
                   f"LABELS: {', '.join(ticket.labels) or 'none'}\nDESCRIPTION:\n{ticket.description}")
    if context := ticket.context_text(max_chars=40_000):
        ticket_text += f"\nCONTEXT:\n{context}"
    messages = [{"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": JUDGE_USER.format(ticket=ticket_text, document=document)}]
    method = f"llm-judge:{judge.name}/{judge.model}"
    try:
        completion = judge.complete(messages)
    except DocgenError as exc:
        log.warning("judge call failed", extra={"error": exc.message})
        return Score(metric="faithfulness", value=None, method=method,
                     notes=[f"judge failed: {exc.message}"]), None

    try:
        verdict = parse_verdict(completion.text)
    except ValueError as exc:
        return Score(metric="faithfulness", value=None, method=method,
                     notes=[str(exc)]), completion.usage

    total = len(verdict.claims)
    unsupported = [c for c in verdict.claims if not c.supported]
    value = 1.0 if total == 0 else (total - len(unsupported)) / total
    notes = [f"{total - len(unsupported)}/{total} claims supported"]
    notes += [f"unsupported: {c.claim} ({c.reason})" if c.reason else f"unsupported: {c.claim}"
              for c in unsupported]
    return Score(metric="faithfulness", value=round(value, 4), method=method,
                 notes=notes), completion.usage
