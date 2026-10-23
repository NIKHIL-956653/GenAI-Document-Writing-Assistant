"""
Command line entry points.

    docgen DEMO-2 --template runbook --publish gdocs   generate (and publish) documents
    docgen --jql "project = DEMO" -j 8 --publish gdocs  batch, concurrent, incremental
    docgen docs                                         what has been published where
    docgen templates                                    list available templates
    docgen eval [--compare] [--judge openrouter]        score models on fixture tickets
    docgen auth google                                  one-time Google sign-in (for servers)
    docgen serve                                        HTTP API + Jira webhook

Exit codes: 0 success, 1 at least one ticket/run failed (or an eval scored
below --min-score), 2 setup problem (bad arguments, configuration, credentials).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from docgen import __version__, config
from docgen.errors import AuthError, ConfigError, DocgenError, InvalidJQLError, InvalidTicketKeyError
from docgen.llm import BACKENDS
from docgen.logging_setup import configure
from docgen.models import ContextLevel, normalise_ticket_key
from docgen.pipeline import DEFAULT_PRICE_IN, DEFAULT_PRICE_OUT, Pipeline, PipelineOptions, log_summary
from docgen.publishers import TARGETS
from docgen.store import Store
from docgen.templates import DEFAULT_TEMPLATE, available_templates, load_template

log = logging.getLogger("docgen.cli")

SETUP_ERRORS = (ConfigError, AuthError, InvalidTicketKeyError, InvalidJQLError)


def _common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--templates-dir", type=Path, default=None,
                    help="extra directory of template YAML files (also DOCGEN_TEMPLATES_DIR)")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    ap.add_argument("--log-format", default="text", choices=["text", "json"],
                    help="json emits one object per line for log shippers")


def _store_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--store", type=Path, default=None,
                    help="SQLite file mapping tickets to documents (default: .docgen/docgen.db, "
                         "or DOCGEN_STORE)")
    ap.add_argument("--no-store", action="store_true",
                    help="don't track documents: every run creates new ones")


def _open_store(args) -> Store | None:
    if getattr(args, "no_store", False):
        return None
    return Store(args.store or config.load(config.DocgenSettings).store)


def _pricing(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--price-in", type=float, default=DEFAULT_PRICE_IN,
                    help="USD per 1M input tokens, used when the provider reports no cost")
    ap.add_argument("--price-out", type=float, default=DEFAULT_PRICE_OUT,
                    help="USD per 1M output tokens, used when the provider reports no cost")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="docgen", description="Jira ticket(s) → LLM → technical document",
        epilog="Other commands: docgen docs | templates | eval | auth google | serve "
               "(each takes --help).")
    ap.add_argument("tickets", nargs="*", default=[],
                    help="One or more Jira issue keys, e.g. DEMO-1 DEMO-2")
    ap.add_argument("--jql", metavar="QUERY",
                    help='also process every issue matching a JQL query, e.g. "project = DEMO AND '
                         'status = Done"')
    ap.add_argument("--max-results", type=int, default=100, metavar="N",
                    help="cap on issues taken from --jql (default: 100)")
    ap.add_argument("--concurrency", "-j", type=int, default=4, metavar="N",
                    help="tickets processed in parallel (default: 4)")
    ap.add_argument("--context", choices=[c.value for c in ContextLevel], default=ContextLevel.STANDARD.value,
                    help="how much Jira context to use: minimal = description only; standard = + "
                         "comments, linked issues, subtasks, attachment names (default); full = + all "
                         "comments, related descriptions, images downloaded and embedded")
    ap.add_argument("--template", "-t", default=DEFAULT_TEMPLATE,
                    help=f"document type (default: {DEFAULT_TEMPLATE}); see `docgen templates`")
    ap.add_argument("--llm", choices=BACKENDS, default="openrouter")
    ap.add_argument("--publish", choices=TARGETS, default="none")
    ap.add_argument("--public", action="store_true",
                    help="with --publish gdocs: share as anyone-with-link")
    ap.add_argument("--review", action="store_true",
                    help="print each document and ask before publishing")
    ap.add_argument("--force", action="store_true",
                    help="regenerate and republish even if the ticket hasn't changed")
    ap.add_argument("--show-diff", action="store_true",
                    help="print a diff against the previously published version")
    _store_args(ap)
    ap.add_argument("--no-progress", action="store_true", help="don't draw the progress bar")
    _pricing(ap)
    ap.add_argument("--output-dir", type=Path, default=None,
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
    if args.concurrency < 1 or args.max_results < 1:
        log.error("--concurrency and --max-results must be at least 1")
        return 2
    try:
        # Validate everything before the first network call or token spent.
        tickets = [normalise_ticket_key(t) for t in args.tickets]
        if not tickets and not args.jql:
            tickets = ["DEMO-1"]
        template = load_template(args.template, args.templates_dir)
        config.preflight(args.llm, args.publish)
        output_dir = args.output_dir or config.load(config.DocgenSettings).output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        store = _open_store(args)

        options = PipelineOptions(
            llm=args.llm, template=template.name, templates_dir=args.templates_dir,
            context=ContextLevel(args.context), publish=args.publish, public=args.public,
            review=args.review, force=args.force, show_diff=args.show_diff,
            concurrency=args.concurrency, price_in=args.price_in, price_out=args.price_out,
            output_dir=output_dir, progress=False if args.no_progress else None)
        pipeline = Pipeline(options, store=store, template=template)
        if args.jql:
            matched = pipeline.jira.search(args.jql, args.max_results)
            if not matched and not tickets:
                log.warning("no issues match the JQL query", extra={"jql": args.jql})
            tickets = list(dict.fromkeys([*tickets, *matched]))

        log.info("starting run", extra={"version": __version__, "tickets": len(tickets),
                                        "template": template.name, "llm": args.llm,
                                        "context": args.context, "publish": args.publish,
                                        "concurrency": args.concurrency,
                                        "store": str(store.path) if store else "off"})
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


def cmd_docs(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="docgen docs",
                                 description="List the documents docgen has published, from the store.")
    ap.add_argument("ticket", nargs="?", help="only this ticket")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    _store_args(ap)
    _common(ap)
    args = ap.parse_args(argv)
    configure(args.log_level, args.log_format)
    try:
        store = _open_store(args)
        if store is None:
            log.error("--no-store makes no sense here")
            return 2
        docs = store.list_documents(normalise_ticket_key(args.ticket) if args.ticket else None)
    except SETUP_ERRORS as exc:
        log.error(str(exc))
        return 2
    if args.json:
        sys.stdout.write(json.dumps([d.model_dump(exclude={"markdown"}) for d in docs], indent=2) + "\n")
    elif not docs:
        sys.stdout.write("No documents recorded yet.\n")
    else:
        from docgen.eval.report import table
        rows = [[d.ticket_key, d.template, d.target, f"v{d.version}", d.updated_at, d.url] for d in docs]
        sys.stdout.write(table(["Ticket", "Template", "Target", "Ver", "Updated", "URL"], rows,
                               align_left=6) + "\n")
    return 0


def cmd_auth(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="docgen auth",
                                 description="Sign in once and save the token, e.g. for the server.")
    ap.add_argument("service", choices=["google"])
    _common(ap)
    args = ap.parse_args(argv)
    configure(args.log_level, args.log_format)
    from docgen.publishers.gdocs import GoogleDocsPublisher
    try:
        publisher = GoogleDocsPublisher()
        publisher.credentials()
    except SETUP_ERRORS as exc:
        log.error(str(exc))
        return 2
    except DocgenError as exc:
        log.error(str(exc))
        return 1
    sys.stdout.write(f"Google token saved to {publisher.settings.token_file.resolve()}\n")
    return 0


def cmd_serve(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="docgen serve",
                                 description="Run the HTTP API and Jira webhook endpoint.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface to bind (default: 127.0.0.1; use 0.0.0.0 in containers)")
    ap.add_argument("--port", type=int, default=8000)
    _common(ap)
    args = ap.parse_args(argv)
    configure(args.log_level, args.log_format)
    try:
        import uvicorn

        from docgen.server import create_app
    except ImportError:
        log.error("the server needs extra packages", extra={"install": 'pip install "docgen[server]"'})
        return 2
    try:
        app = create_app(templates_dir=args.templates_dir)
    except SETUP_ERRORS as exc:
        log.error(str(exc))
        return 2
    log.info("serving", extra={"url": f"http://{args.host}:{args.port}", "docs": "/docs",
                               "version": __version__})
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)
    return 0


COMMANDS = {"eval": cmd_eval, "templates": cmd_templates, "docs": cmd_docs, "auth": cmd_auth,
            "serve": cmd_serve}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] in COMMANDS:
            return COMMANDS[argv[0]](argv[1:])
        return cmd_generate(argv)
    except DocgenError as exc:  # pragma: no cover — last-resort guard, never a traceback
        log.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
