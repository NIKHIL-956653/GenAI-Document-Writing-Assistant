"""
Run fixture tickets through generation for one or more models and score the output.

Fixtures are interleaved across models (ticket 1 on every model, then ticket
2, ...) so a comparison run that is interrupted still has paired results.
"""
from __future__ import annotations

import logging
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from docgen.errors import ConfigError, DocgenError, InvalidTicketKeyError
from docgen.eval.judge import judge_faithfulness
from docgen.eval.scorers import Score, completeness, faithfulness, structure
from docgen.llm import BACKENDS, Backend, generate, get_backend
from docgen.models import Ticket, TokenUsage
from docgen.templates import Template, load_template

log = logging.getLogger(__name__)

FIXTURES_FILE = Path(__file__).parent / "fixtures" / "tickets.yaml"
METRICS = ("faithfulness", "completeness", "structure")


# ── inputs ───────────────────────────────────────────────────

class Fixture(BaseModel):
    id: str
    template: str
    ticket: Ticket


def load_fixtures(path: Path = FIXTURES_FILE, only: list[str] | None = None) -> list[Fixture]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Fixture file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"Fixture file {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, list):
        raise ConfigError(f"Fixture file {path} must be a YAML list of fixtures")
    try:
        fixtures = [Fixture.model_validate(item) for item in data]
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ConfigError(f"Fixture file {path} is invalid: {problems}") from None
    except InvalidTicketKeyError as exc:
        raise ConfigError(f"Fixture file {path}: {exc.message}", hint=exc.hint) from None
    if only:
        unknown = set(only) - {f.id for f in fixtures}
        if unknown:
            raise ConfigError(f"Unknown fixture id(s): {', '.join(sorted(unknown))}",
                              hint="Available: " + ", ".join(f.id for f in fixtures))
        fixtures = [f for f in fixtures if f.id in only]
    return fixtures


class ModelSpec(BaseModel):
    """`openrouter`, `ollama:llama3.1`, `openrouter:meta-llama/llama-3-8b-instruct:free`"""
    backend: str
    model: str | None = None

    @classmethod
    def parse(cls, spec: str) -> ModelSpec:
        backend, _, model = spec.partition(":")
        if backend not in BACKENDS:
            raise ConfigError(f"Unknown backend in model spec '{spec}'",
                              hint=f"Use BACKEND[:MODEL] with BACKEND one of {', '.join(BACKENDS)}")
        return cls(backend=backend, model=model or None)

    def build(self) -> Backend:
        return get_backend(self.backend, self.model)


# ── outputs ──────────────────────────────────────────────────

class EvalRecord(BaseModel):
    fixture: str
    template: str
    model: str
    scores: dict[str, Score] = Field(default_factory=dict)
    overall: float | None = None
    usage: TokenUsage | None = None
    cost_usd: float | None = None
    judge_usage: TokenUsage | None = None
    judge_cost_usd: float | None = None
    seconds: float = 0.0
    error: str | None = None
    document: str | None = None

    def value(self, metric: str) -> float | None:
        if metric == "overall":
            return self.overall
        score = self.scores.get(metric)
        return score.value if score else None


class ModelSummary(BaseModel):
    model: str
    runs: int
    errors: int
    means: dict[str, float | None]
    total_tokens: int | None
    total_cost_usd: float | None
    judge_cost_usd: float | None
    mean_seconds: float | None


class EvalReport(BaseModel):
    created_at: str
    fixtures_file: str
    models: list[str]
    judge: str | None = None
    records: list[EvalRecord]
    summaries: list[ModelSummary]

    def records_for(self, model: str) -> list[EvalRecord]:
        return [r for r in self.records if r.model == model]


def model_label(backend: Backend) -> str:
    return f"{backend.name}:{backend.model}"


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), 4) if values else None


def _sum(values) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def summarise(model: str, records: list[EvalRecord]) -> ModelSummary:
    ok = [r for r in records if r.error is None]
    tokens = _sum(r.usage.total_tokens for r in ok if r.usage)
    total_cost = _sum(r.cost_usd for r in ok)
    return ModelSummary(
        model=model, runs=len(records), errors=len(records) - len(ok),
        means={m: _mean(r.value(m) for r in ok) for m in (*METRICS, "overall")},
        total_tokens=int(tokens) if tokens is not None else None,
        total_cost_usd=round(total_cost, 6) if total_cost is not None else None,
        judge_cost_usd=_sum(r.judge_cost_usd for r in ok),
        mean_seconds=_mean(r.seconds for r in ok),
    )


# ── run ──────────────────────────────────────────────────────

def score_document(document: str, fixture: Fixture, template: Template,
                   judge: Backend | None = None) -> tuple[dict[str, Score], TokenUsage | None]:
    """All metrics for one document. With a judge, its verdict is the faithfulness score
    and the heuristic is kept alongside as `faithfulness_heuristic`."""
    heuristic = faithfulness(document, fixture.ticket, template)
    scores = {"faithfulness": heuristic,
              "completeness": completeness(document, template),
              "structure": structure(document, template)}
    judge_usage = None
    if judge is not None:
        judged, judge_usage = judge_faithfulness(judge, document, fixture.ticket)
        if judged.value is not None:
            scores["faithfulness"] = judged
            scores["faithfulness_heuristic"] = heuristic
        else:
            scores["faithfulness_judge"] = judged
    return scores, judge_usage


def run_eval(
    fixtures: list[Fixture],
    backends: list[Backend],
    *,
    judge: Backend | None = None,
    price_in: float = 0.0,
    price_out: float = 0.0,
    templates_dir: Path | None = None,
    fixtures_file: Path = FIXTURES_FILE,
) -> EvalReport:
    templates: dict[str, Template] = {}
    for fixture in fixtures:  # fail on a bad template before spending any tokens
        if fixture.template not in templates:
            templates[fixture.template] = load_template(fixture.template, templates_dir)

    records = []
    for fixture in fixtures:
        template = templates[fixture.template]
        for backend in backends:
            records.append(_run_one(fixture, template, backend, judge, price_in, price_out))

    labels = [model_label(b) for b in backends]
    return EvalReport(
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        fixtures_file=str(fixtures_file),
        models=labels,
        judge=model_label(judge) if judge else None,
        records=records,
        summaries=[summarise(label, [r for r in records if r.model == label]) for label in labels],
    )


def _run_one(fixture: Fixture, template: Template, backend: Backend, judge: Backend | None,
             price_in: float, price_out: float) -> EvalRecord:
    label = model_label(backend)
    record = EvalRecord(fixture=fixture.id, template=template.name, model=label)
    log.info("eval run", extra={"fixture": fixture.id, "template": template.name, "model": label})
    started = time.monotonic()
    try:
        document = generate(backend, fixture.ticket, template)
    except ConfigError:
        raise
    except DocgenError as exc:  # recorded per run; other models/fixtures still run
        record.error = exc.message
        record.seconds = time.monotonic() - started
        log.error("eval generation failed", extra={"fixture": fixture.id, "model": label,
                                                   "error": exc.message})
        return record
    record.seconds = time.monotonic() - started
    record.document = document.body
    record.usage = document.usage
    record.cost_usd = document.usage.cost(price_in, price_out)

    record.scores, record.judge_usage = score_document(document.body, fixture, template, judge)
    if record.judge_usage is not None:
        record.judge_cost_usd = record.judge_usage.cost(price_in, price_out)
    record.overall = _mean(record.value(m) for m in METRICS)
    log.info("eval scored", extra={"fixture": fixture.id, "model": label,
                                   **{m: record.value(m) for m in (*METRICS, "overall")},
                                   "tokens": document.usage.total_tokens,
                                   "cost_usd": record.cost_usd})
    return record
