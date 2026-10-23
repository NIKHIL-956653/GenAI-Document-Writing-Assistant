"""
Command line entry point: `docgen DEMO-2 --publish gdocs`.

Exit codes: 0 all tickets succeeded, 1 at least one ticket failed,
2 setup problem (bad arguments, configuration, credentials).
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from docgen import __version__, config
from docgen.errors import AuthError, ConfigError, DocgenError, InvalidTicketKeyError
from docgen.llm import BACKENDS
from docgen.logging_setup import configure
from docgen.models import normalise_ticket_key
from docgen.pipeline import DEFAULT_PRICE_IN, DEFAULT_PRICE_OUT, Pipeline, log_summary
from docgen.publishers import TARGETS

log = logging.getLogger("docgen.cli")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="docgen",
                                 description="Jira ticket(s) → LLM → technical document")
    ap.add_argument("tickets", nargs="*", default=["DEMO-1"],
                    help="One or more Jira issue keys, e.g. DEMO-1 DEMO-2")
    ap.add_argument("--llm", choices=BACKENDS, default="openrouter")
    ap.add_argument("--publish", choices=TARGETS, default="none")
    ap.add_argument("--public", action="store_true",
                    help="with --publish gdocs: share as anyone-with-link")
    ap.add_argument("--review", action="store_true",
                    help="print each document and ask before publishing")
    ap.add_argument("--price-in", type=float, default=DEFAULT_PRICE_IN,
                    help="USD per 1M input tokens, for cost reporting")
    ap.add_argument("--price-out", type=float, default=DEFAULT_PRICE_OUT,
                    help="USD per 1M output tokens, for cost reporting")
    ap.add_argument("--output-dir", type=Path, default=Path("."),
                    help="where <TICKET>_document.md files are written (default: cwd)")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    ap.add_argument("--log-format", default="text", choices=["text", "json"],
                    help="json emits one object per line for log shippers")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure(args.log_level, args.log_format)

    try:
        # Validate everything before the first network call or token spent.
        tickets = [normalise_ticket_key(t) for t in (args.tickets or ["DEMO-1"])]
        config.preflight(args.llm, args.publish)
        args.output_dir.mkdir(parents=True, exist_ok=True)

        log.info("starting run", extra={"version": __version__, "tickets": len(tickets),
                                        "llm": args.llm, "publish": args.publish})
        pipeline = Pipeline(llm=args.llm, publish=args.publish, public=args.public,
                            review=args.review, price_in=args.price_in,
                            price_out=args.price_out, output_dir=args.output_dir)
        results = pipeline.run(tickets)
    except (ConfigError, AuthError, InvalidTicketKeyError) as exc:
        log.error(str(exc))
        return 2
    except DocgenError as exc:  # pragma: no cover — per-ticket errors are caught in the pipeline
        log.error(str(exc))
        return 1
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130

    log_summary(results)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
