"""
Command line entry points.

    docgen DEMO-2 --template runbook --publish gdocs   generate (and publish) documents
    docgen templates                                    list available templates
    docgen eval [--compare] [--judge openrouter]        score models on fixture tickets

Exit codes: 0 success, 1 at least one ticket/run failed (or an eval scored
below --min-score), 2 setup problem (bad arguments, configuration, credentials).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from docgen import __version__, config
from docgen.errors import AuthError, ConfigError, DocgenError, InvalidTicketKeyError
from docgen.llm import BACKENDS
from docgen.logging_setup import configure
from docgen.models import normalise_ticket_key
from docgen.pipeline import DEFAULT_PRICE_IN, DEFAULT_PRICE_OUT, Pipeline, log_summary
from docgen.publishers import TARGETS
from docgen.templates import DEFAULT_TEMPLATE, available_templates, load_template

log = logging.getLogger("docgen.cli")

SETUP_ERRORS = (ConfigError, AuthError, InvalidTicketKeyError)


def _common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--templates-dir", type=Path, default=None,
                    help="extra directory of template YAML files (also DOCGEN_TEMPLATES_DIR)")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    ap.add_argument("--log-format", default="text", choices=["text", "json"],
                    help="json emits one object per line for log shippers")


def _pricing(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--price-in", type=float, default=DEFAULT_PRICE_IN,
                    help="USD per 1M input tokens, used when the provider reports no cost")
    ap.add_argument("--price-out", type=float, default=DEFAULT_PRICE_OUT,
                    help="USD per 1M output tokens, used when the provider reports no cost")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="docgen", description="Jira ticket(s) → LLM → technical document",
        epilog="Other commands: `docgen templates` lists templates; "
               "`docgen eval --help` scores models on fixture tickets.")
    ap.add_argument("tickets", nargs="*", default=["DEMO-1"],
                    help="One or more Jira issue keys, e.g. DEMO-1 DEMO-2")
    ap.add_argument("--template", "-t", default=DEFAULT_TEMPLATE,
                    help=f"document type (default: {DEFAULT_TEMPLATE}); see `docgen templates`")
    ap.add_argument("--llm", choices=BACKENDS, default="openrouter")
    ap.add_argument("--publish", choices=TARGETS, default="none")
    ap.add_argument("--public", action="store_true",
                    help="with --publish gdocs: share as anyone-with-link")
    ap.add_argument("--review", action="store_true",
                    help="print each document and ask before publishing")
    _pricing(ap)
    ap.add_argument("--output-dir", type=Path, default=Path("."),
                    help="where <TICKET>_document.md files are written (default: cwd)")
    _common(ap)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap


def build_eval_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="docgen eval",
        description="Run fixture tickets through one or more models and score the documents "
                    "for faithfulness, completeness and structure.")
    ap.add_argument("--models", nargs="+", metavar="BACKEND[:MODEL]",
                    help="models to evaluate, e.g. openrouter ollama:llama3.1 "
                         "(default: openrouter; with --compare: openrouter ollama)")
    ap.add_argument("--compare", action="store_true",
                    help="show every fixture's scores side by side across models")
    ap.add_argument("--judge", metavar="BACKEND[:MODEL]",
                    help="also score faithfulness with an LLM judge (costs tokens)")
    ap.add_argument("--fixtures", type=Path, default=None,
                    help="fixture YAML file (default: the built-in fixtures)")
    ap.add_argument("--only", nargs="+", metavar="ID", help="run only these fixture ids")
    ap.add_argument("--output", type=Path, help="also write the full report as JSON")
    ap.add_argument("--save-docs", type=Path, metavar="DIR",
                    help="write each generated document to DIR/<fixture>__<model>.md")
    ap.add_argument("--details", action="store_true",
                    help="list the failed checks and unsupported claims behind each score")
    ap.add_argument("--min-score", type=float, metavar="X",
                    help="exit 1 if any model's mean overall score is below X (CI gate)")
    _pricing(ap)
    _common(ap)
    return ap


# ── commands ─────────────────────────────────────────────────

def cmd_generate(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    configure(args.log_level, args.log_format)
    try:
        # Validate everything before the first network call or token spent.
        tickets = [normalise_ticket_key(t) for t in (args.tickets or ["DEMO-1"])]
        template = load_template(args.template, args.templates_dir)
        config.preflight(args.llm, args.publish)
        args.output_dir.mkdir(parents=True, exist_ok=True)

        log.info("starting run", extra={"version": __version__, "tickets": len(tickets),
                                        "template": template.name, "llm": args.llm,
                                        "publish": args.publish})
        pipeline = Pipeline(llm=args.llm, template=template, publish=args.publish,
                            public=args.public, review=args.review, price_in=args.price_in,
                            price_out=args.price_out, output_dir=args.output_dir)
        results = pipeline.run(tickets)
    except SETUP_ERRORS as exc:
        log.error(str(exc))
        return 2
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130

    log_summary(results)
    return 0 if all(r.ok for r in results) else 1


def cmd_templates(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="docgen templates", description="List document templates.")
    _common(ap)
    args = ap.parse_args(argv)
    configure(args.log_level, args.log_format)
    try:
        rows = []
        for name in available_templates(args.templates_dir):
            t = load_template(name, args.templates_dir)
            sections = ", ".join(s.title for s in t.sections)
            rows.append(f"{name:<14} {t.title}\n{'':<14} {t.description}\n"
                        f"{'':<14} sections: {sections}\n{'':<14} source: {t.source}")
    except ConfigError as exc:
        log.error(str(exc))
        return 2
    sys.stdout.write("\n\n".join(rows) + "\n")
    return 0


def cmd_eval(argv: list[str]) -> int:
    from docgen.eval import ModelSpec, load_fixtures, run_eval
    from docgen.eval.report import render
    from docgen.eval.runner import FIXTURES_FILE

    args = build_eval_parser().parse_args(argv)
    configure(args.log_level, args.log_format)

    specs_text = args.models or (["openrouter", "ollama"] if args.compare else ["openrouter"])
    if args.compare and len(specs_text) < 2:
        log.error("--compare needs at least two models, e.g. --models openrouter ollama")
        return 2

    try:
        specs = [ModelSpec.parse(s) for s in specs_text]
        judge_spec = ModelSpec.parse(args.judge) if args.judge else None
        fixtures_file = args.fixtures or FIXTURES_FILE
        fixtures = load_fixtures(fixtures_file, args.only)
        config.preflight_backends([s.backend for s in specs]
                                  + ([judge_spec.backend] if judge_spec else []))
        backends = [s.build() for s in specs]
        judge = judge_spec.build() if judge_spec else None

        log.info("starting eval", extra={"fixtures": len(fixtures), "models": len(backends),
                                         "judge": args.judge or "none"})
        report = run_eval(fixtures, backends, judge=judge, price_in=args.price_in,
                          price_out=args.price_out, templates_dir=args.templates_dir,
                          fixtures_file=fixtures_file)
    except SETUP_ERRORS as exc:
        log.error(str(exc))
        return 2
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130

    sys.stdout.write(render(report, compare=args.compare, details=args.details))

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        log.info("report written", extra={"path": str(args.output)})
    if args.save_docs:
        args.save_docs.mkdir(parents=True, exist_ok=True)
        for r in report.records:
            if r.document:
                safe = r.model.replace("/", "_").replace(":", "_")
                (args.save_docs / f"{r.fixture}__{safe}.md").write_text(r.document,
                                                                          encoding="utf-8")

    failed = any(r.error for r in report.records)
    below = [s.model for s in report.summaries
             if args.min_score is not None
             and (s.means["overall"] is None or s.means["overall"] < args.min_score)]
    if below:
        log.error("below --min-score", extra={"models": ", ".join(below),
                                              "min_score": args.min_score})
    return 1 if failed or below else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] == "eval":
            return cmd_eval(argv[1:])
        if argv and argv[0] == "templates":
            return cmd_templates(argv[1:])
        return cmd_generate(argv)
    except DocgenError as exc:  # pragma: no cover — last-resort guard, never a traceback
        log.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
