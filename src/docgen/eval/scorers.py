"""
Deterministic scorers: faithfulness, completeness, structure. Each returns a
Score in [0, 1] with the individual checks behind it, so a low number always
comes with the reason.

These need no network and cost nothing, so they run on every eval and in CI.
Faithfulness here is a *proxy* — see `faithfulness()` — and the LLM judge in
judge.py is the stronger (paid) measurement of the same property.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from docgen.eval.markdown import (
    FENCE_RE,
    ParsedDocument,
    Section,
    count_words,
    match_template,
    matches,
    parse,
)
from docgen.models import Ticket
from docgen.templates import Format, Template


class Check(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class Score(BaseModel):
    metric: str
    value: float | None = Field(description="0..1, or None when it could not be scored")
    method: str = "heuristic"
    checks: list[Check] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ── faithfulness ─────────────────────────────────────────────

URL_RE = re.compile(r"https?://[^\s)>\]`'\"]+")
PATH_RE = re.compile(r"(?<![\w/<])/(?:[A-Za-z0-9_.{}:-]+/)*[A-Za-z0-9_.{}:-]*[A-Za-z][A-Za-z0-9_.{}:-]*")
CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
NUMBER_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w.]*[A-Za-z]{3,})")
# Two or more capitals: acronyms (JWT, SAML) and CamelCase names (PostgreSQL, GraphQL)
ACRONYM_RE = re.compile(r"\b[A-Za-z]*[A-Z][a-z0-9]*[A-Z][A-Za-z0-9]*\b")
# A capitalised word that does not start a sentence, list item or label: a likely proper noun
PROPER_RE = re.compile(r"(?<=[a-z0-9,;)] )[A-Z][a-z]{2,}\b")
PLACEHOLDER_RE = re.compile(r"<[^>\n]+>|\{[^}\n]+\}")
COUNTER_WORDS = re.compile(r"\b(step|phase|stage|option|part|section|item)\s+$", re.IGNORECASE)

# "Standard engineering practice" the prompt explicitly permits: generic protocol and
# document vocabulary, and the common HTTP status codes.
GENERIC_TERMS = {
    "api", "apis", "http", "https", "rest", "json", "xml", "yaml", "url", "urls", "uri", "ui",
    "ux", "id", "ids", "uuid", "ci", "cd", "sql", "tls", "ssl", "dns", "cpu", "ram", "sla", "slo",
    "sli", "kpi", "kpis", "pr", "prs", "qa", "mvp", "faq", "tbd", "ok", "utc", "iso", "get",
    "post", "put", "patch", "delete", "crud", "sdk", "cli", "os", "ip", "tcp", "e2e", "html",
    "markdown", "md", "n/a",
}
GENERIC_NUMBERS = {"0", "1", "2", "200", "201", "204", "400", "401", "403", "404", "409", "422",
                   "429", "500", "502", "503"}


def _ticket_corpus(ticket: Ticket) -> str:
    return " ".join([ticket.key, ticket.title, ticket.description, ticket.status,
                     *ticket.labels]).lower()


def _grounded_text(term: str, corpus: str) -> bool:
    term = term.lower().strip()
    if not term or term in GENERIC_TERMS:
        return True
    if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", corpus):
        return True
    # Multi-word code spans (`POST /login`) are grounded when every word is.
    words = re.findall(r"[a-z0-9][a-z0-9_.-]*", term)
    return len(words) > 1 and all(w in GENERIC_TERMS or w in corpus for w in words)


def extract_specifics(text: str) -> list[tuple[str, str]]:
    """
    (kind, value) pairs for the checkable specifics in a passage: URLs, paths,
    code identifiers, numbers, acronyms and mid-sentence proper nouns. Fenced
    code is skipped except for URLs and paths — example payloads are illustrative.
    """
    found: list[tuple[str, str]] = []
    prose_lines, in_fence = [], False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            found += [("url", u) for u in URL_RE.findall(line)]
            found += [("path", p) for p in PATH_RE.findall(URL_RE.sub(" ", line))]
        else:
            prose_lines.append(line)

    prose = "\n".join(prose_lines)
    prose = PLACEHOLDER_RE.sub(" ", prose)
    found += [("url", u) for u in URL_RE.findall(prose)]
    prose = URL_RE.sub(" ", prose)
    for span in CODE_SPAN_RE.findall(prose):
        found.append(("code", span.strip()))
    prose = CODE_SPAN_RE.sub(" ", prose)
    found += [("path", p) for p in PATH_RE.findall(prose)]
    prose = PATH_RE.sub(" ", prose)
    # Strip list markers, emphasis and table pipes so they don't read as numbers or sentence ends.
    prose = re.sub(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+", "", prose).replace("**", "").replace("|", " ")

    for m in NUMBER_RE.finditer(prose):
        if not COUNTER_WORDS.search(prose[max(0, m.start() - 12):m.start()]):
            found.append(("number", m.group().replace(",", "")))
    found += [("term", t) for t in ACRONYM_RE.findall(prose)]
    found += [("term", t) for t in PROPER_RE.findall(prose)]
    return found


def faithfulness(document: str, ticket: Ticket, template: Template) -> Score:
    """
    Entity-grounding proxy for "no claims absent from the ticket".

    LLM fabrications are overwhelmingly *specifics* — a number, an endpoint, a
    product name, a config key — so every such specific in the document is
    looked up in the ticket. Score = grounded / total distinct specifics.
    Generic vocabulary the prompt permits ("standard engineering practice")
    and template section names are allowed; the template's unknowns section is
    skipped, since "the timeout is not specified" is not a claim.
    It cannot see unsupported claims made in ordinary words — use --judge.
    """
    doc = parse(document)
    unknowns = template.unknowns_section
    skip_lines: set[int] = set()
    if unknowns:
        for section in doc.sections:
            if matches(unknowns, section.heading):
                skip_lines.update(range(section.line, section.line + len(section.body_lines) + 1))

    lines = document.splitlines()
    kept = [line for i, line in enumerate(lines, start=1)
            if i not in skip_lines and not re.match(r"^#{1,6}\s", line)]
    corpus = _ticket_corpus(ticket)
    template_terms = " ".join(" ".join([s.title, *s.aliases]) for s in template.sections).lower()
    ticket_numbers = {n.replace(",", "") for n in NUMBER_RE.findall(corpus)}

    distinct: dict[tuple[str, str], tuple[str, bool]] = {}
    for kind, value in extract_specifics("\n".join(kept)):
        key = (kind, value.lower())
        if key in distinct:
            continue
        if kind == "number":
            ok = value in GENERIC_NUMBERS or value in ticket_numbers
        elif kind in ("url", "path"):
            ok = value.lower().rstrip(".,") in corpus
        else:
            ok = _grounded_text(value, corpus) or value.lower() in template_terms
        distinct[key] = (value, ok)

    total = len(distinct)
    unsupported = [f"{kind}: {original}" for (kind, _), (original, ok) in distinct.items()
                   if not ok]
    grounded = 1.0 if total == 0 else (total - len(unsupported)) / total
    notes = [f"{total - len(unsupported)}/{total} specifics found in the ticket"]
    notes += [f"unsupported {u}" for u in unsupported]
    return Score(metric="faithfulness", value=round(grounded, 4), method="entity-grounding",
                 notes=notes)


# ── completeness ─────────────────────────────────────────────

PLACEHOLDER_BODIES = {"tbd", "to be determined", "todo", "n a", "na", "not applicable",
                      "coming soon", "none", "no", "nothing", "placeholder", "lorem ipsum"}


def _is_placeholder(section: Section) -> bool:
    text = re.sub(r"[^a-z ]+", " ", section.body.lower())
    return " ".join(text.split()) in PLACEHOLDER_BODIES


def completeness(document: str, template: Template) -> Score:
    """
    Required sections present *and* non-trivial: at least `min_words` words and
    not a placeholder like "TBD". Present-but-trivial earns half credit.
    Optional sections are reported but not scored.
    """
    found = match_template(parse(document), template)
    checks, points, required = [], 0.0, 0
    for spec in template.sections:
        section = found[spec.title]
        if section is None:
            if spec.required:
                required += 1
                checks.append(Check(name=spec.title, passed=False, detail="missing"))
            else:
                checks.append(Check(name=spec.title, passed=True, detail="optional, absent"))
            continue

        words = section.words
        if spec.min_words == 0:
            trivial = not section.body
            why = "empty"
        elif _is_placeholder(section):
            trivial, why = True, f"placeholder text {section.body!r}"
        else:
            trivial = words < spec.min_words
            why = f"{words} words < {spec.min_words}"
        if spec.required:
            required += 1
            points += 0.5 if trivial else 1.0
        checks.append(Check(name=spec.title, passed=not trivial,
                            detail=why if trivial else f"{words} words"))

    value = points / required if required else 1.0
    return Score(metric="completeness", value=round(value, 4), checks=checks)


# ── structure ────────────────────────────────────────────────

def _format_ok(section: Section, formats: list[Format]) -> bool:
    tests = {
        Format.ANY: lambda: True,
        Format.PROSE: section.has_prose,
        Format.LIST: section.has_bullets,
        Format.ORDERED_LIST: section.has_ordered_list,
        Format.TABLE: section.has_table,
        Format.CODE: section.has_code_block,
    }
    return any(tests[f]() for f in formats)


def structure(document: str, template: Template) -> Score:
    """Does the document follow the template's output schema?"""
    doc: ParsedDocument = parse(document)
    level = template.output.heading_level
    found = match_template(doc, template)
    present = [(spec, s) for spec in template.sections if (s := found[spec.title]) is not None]
    checks: list[Check] = []

    preamble = [line for line in doc.preamble if line.strip()]
    checks.append(Check(name="no_preamble", passed=not preamble,
                        detail=f"{len(preamble)} line(s) before the first heading" if preamble
                        else ""))

    wrong_level = [f"{s.heading} (h{s.level})" for _, s in present if s.level != level]
    checks.append(Check(name="heading_level", passed=not wrong_level,
                        detail=f"expected h{level}: " + ", ".join(wrong_level) if wrong_level
                        else ""))

    lines = [s.line for _, s in present]
    in_order = lines == sorted(lines)
    actual = sorted(present, key=lambda pair: pair[1].line)
    checks.append(Check(name="section_order", passed=in_order,
                        detail="" if in_order else "order: " + " → ".join(s.heading for _, s in actual)))

    dupes = [spec.title for spec in template.sections
             if sum(matches(spec, s.heading) for s in doc.at_level(level)) > 1]
    checks.append(Check(name="no_duplicate_sections", passed=not dupes, detail=", ".join(dupes)))

    checks.append(Check(name="code_fences_closed", passed=not doc.unclosed_fence))

    if not template.output.allow_extra_sections:
        extra = [s.heading for s in doc.at_level(level)
                 if not any(matches(spec, s.heading) for spec in template.sections)]
        checks.append(Check(name="no_extra_sections", passed=not extra, detail=", ".join(extra)))

    if template.output.max_words:
        words = count_words(document)
        checks.append(Check(name="max_words", passed=words <= template.output.max_words,
                            detail=f"{words} / {template.output.max_words}"))

    for spec, section in present:
        if spec.format != [Format.ANY]:
            ok = _format_ok(section, spec.format)
            checks.append(Check(name=f"format:{spec.title}", passed=ok,
                                detail="" if ok else "expected " + " or ".join(f.value for f in spec.format)))

    value = sum(c.passed for c in checks) / len(checks)
    return Score(metric="structure", value=round(value, 4), checks=checks)
