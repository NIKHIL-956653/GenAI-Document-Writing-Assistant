"""Plain-text rendering of an EvalReport: per-model summary, per-run scores, side-by-side comparison."""
from __future__ import annotations

from docgen.eval.runner import METRICS, EvalRecord, EvalReport

SHORT = {"faithfulness": "Faith", "completeness": "Compl", "structure": "Struct",
         "overall": "Overall"}


def _f(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _cost(value: float | None) -> str:
    return "—" if value is None else f"${value:.5f}"


def _int(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def table(headers: list[str], rows: list[list[str]], align_left: int = 1) -> str:
    """Fixed-width table; the first `align_left` columns are left-aligned, the rest right."""
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
              for i, h in enumerate(headers)]

    def line(cells):
        return "  ".join(c.ljust(w) if i < align_left else c.rjust(w)
                         for i, (c, w) in enumerate(zip(cells, widths, strict=True)))

    rule = "  ".join("─" * w for w in widths)
    return "\n".join([line(headers), rule, *(line(r) for r in rows)])


def summary_table(report: EvalReport) -> str:
    rows = []
    for s in report.summaries:
        rows.append([s.model, f"{s.runs - s.errors}/{s.runs}",
                     *(_f(s.means[m]) for m in (*METRICS, "overall")),
                     _int(s.total_tokens), _cost(s.total_cost_usd),
                     "—" if s.mean_seconds is None else f"{s.mean_seconds:.1f}s"])
    headers = ["Model", "OK", *(SHORT[m] for m in (*METRICS, "overall")), "Tokens", "Cost",
               "Avg time"]
    return table(headers, rows)


def runs_table(report: EvalReport) -> str:
    rows = []
    for r in report.records:
        if r.error:
            rows.append([r.fixture, r.template, r.model, "error", "", "", "", "", "", ""])
            continue
        rows.append([r.fixture, r.template, r.model,
                     *(_f(r.value(m)) for m in (*METRICS, "overall")),
                     _int(r.usage.total_tokens if r.usage else None), _cost(r.cost_usd),
                     f"{r.seconds:.1f}s"])
    headers = ["Fixture", "Template", "Model", *(SHORT[m] for m in (*METRICS, "overall")),
               "Tokens", "Cost", "Time"]
    return table(headers, rows, align_left=3)


def comparison_table(report: EvalReport) -> str:
    """One block per fixture, one column per model; the best score in each row is marked *."""
    by_key: dict[tuple[str, str], EvalRecord] = {(r.fixture, r.model): r for r in report.records}
    fixtures = list(dict.fromkeys(r.fixture for r in report.records))
    rows: list[list[str]] = []
    for fixture in fixtures:
        records = [by_key.get((fixture, m)) for m in report.models]
        for n, metric in enumerate((*METRICS, "overall", "tokens", "cost")):
            cells = []
            if metric in ("tokens", "cost"):
                for r in records:
                    if r is None or r.error:
                        cells.append("—")
                    elif metric == "tokens":
                        cells.append(_int(r.usage.total_tokens if r.usage else None))
                    else:
                        cells.append(_cost(r.cost_usd))
            else:
                values = [None if r is None or r.error else r.value(metric) for r in records]
                present = [v for v in values if v is not None]
                best = max(present) if len(present) > 1 and len(set(present)) > 1 else None
                cells = ["error" if r is not None and r.error else
                         _f(v) + ("*" if v is not None and v == best else " ")
                         for r, v in zip(records, values, strict=True)]
            rows.append([fixture if n == 0 else "", SHORT.get(metric, metric.capitalize()), *cells])
        rows.append([""] * (2 + len(report.models)))
    return table(["Fixture", "Metric", *report.models], rows[:-1], align_left=2)


def findings(report: EvalReport, limit: int = 5) -> str:
    """Why scores are below 1: failed checks and unsupported specifics, per run."""
    out = []
    for r in report.records:
        if r.error:
            out.append(f"{r.fixture} · {r.model}\n  error: {r.error}")
            continue
        items = []
        for name, score in r.scores.items():
            items += [f"{name}: {c.name} — {c.detail or 'failed'}" for c in score.checks
                      if not c.passed]
            items += [f"{name}: {n}" for n in score.notes if n.startswith("unsupported")]
            items += [f"{name}: {n}" for n in score.notes if n.startswith("judge failed")]
        if items:
            shown = items[:limit] + ([f"… {len(items) - limit} more"] if len(items) > limit else [])
            out.append(f"{r.fixture} · {r.model}\n" + "\n".join(f"  - {i}" for i in shown))
    return "\n\n".join(out) if out else "No findings — every check passed."


def render(report: EvalReport, *, compare: bool = False, details: bool = False) -> str:
    parts = ["Scores by model", summary_table(report)]
    if compare and len(report.models) > 1:
        parts += ["", "Side by side", comparison_table(report)]
    else:
        parts += ["", "Per run", runs_table(report)]
    if report.judge:
        parts += ["", f"Faithfulness judged by {report.judge}"
                      + (f" · judge cost {_cost(sum(s.judge_cost_usd or 0 for s in report.summaries))}")]
    if details:
        parts += ["", "Findings", findings(report)]
    return "\n".join(parts) + "\n"
