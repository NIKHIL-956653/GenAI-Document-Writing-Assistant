"""
Jira ticket → LLM → technical document → Google Docs / Confluence.

Batch-capable, with an optional human review gate before anything is
published, and a run summary that reports real timing and token usage so
performance claims come from measurements rather than guesses.
"""
import argparse
import time

import llm_generate
from jira_fetch import get_jira_ticket
from llm_generate import generate_document_openrouter, generate_document_ollama

# OpenRouter price per 1M tokens for the default model (input/output).
# Override with --price-in / --price-out when you switch models.
DEFAULT_PRICE_IN = 0.075
DEFAULT_PRICE_OUT = 0.30


def estimate_cost(usage: dict, price_in: float, price_out: float):
    """Cost in USD from token counts, or None if the backend reported none."""
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None or completion is None:
        return None
    return (prompt / 1_000_000 * price_in) + (completion / 1_000_000 * price_out)


def confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        # Non-interactive (CI, piped input) — never publish without a human
        print("  (no TTY — skipping publish)")
        return False


def process_ticket(
    ticket_id: str,
    llm: str = "openrouter",
    publish: str = "none",
    public: bool = False,
    review: bool = False,
    price_in: float = DEFAULT_PRICE_IN,
    price_out: float = DEFAULT_PRICE_OUT,
) -> dict:
    """Run the pipeline for one ticket. Returns a result record for the summary."""
    result = {"ticket": ticket_id, "ok": False, "chars": 0,
              "seconds": 0.0, "usage": {}, "cost": None, "url": ""}
    started = time.time()

    print(f"\n{'='*60}")
    print(f"  {ticket_id}")
    print(f"{'='*60}")

    # Step 1 — fetch
    print(f"\n[1/4] Fetching Jira ticket {ticket_id}...")
    ticket = get_jira_ticket(ticket_id)
    if not ticket:
        print("  FAILED: could not fetch ticket")
        result["seconds"] = time.time() - started
        return result
    print(f"  OK: {ticket['title']}")

    # Step 2 — generate
    print(f"\n[2/4] Generating document with {llm}...")
    generate = generate_document_openrouter if llm == "openrouter" else generate_document_ollama
    document = generate(ticket)
    if not document:
        print("  FAILED: no document returned")
        result["seconds"] = time.time() - started
        return result

    usage = dict(llm_generate.LAST_USAGE)
    cost = estimate_cost(usage, price_in, price_out)
    tokens = usage.get("total_tokens")
    print(f"  OK: {len(document)} chars"
          + (f", {tokens} tokens" if tokens else "")
          + (f", ${cost:.6f}" if cost is not None else ""))

    # Step 3 — save locally (always, before any publish can fail)
    filename = f"{ticket_id}_document.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# {ticket['title']}\n\n")
        f.write(document)
    print(f"\n[3/4] Saved {filename}")

    # Step 4 — publish, optionally behind a review gate
    if publish == "none":
        print("\n[4/4] Publish skipped (--publish none)")
    else:
        approved = True
        if review:
            print(f"\n{'-'*60}")
            print(document)
            print(f"{'-'*60}")
            approved = confirm(f"Publish {ticket_id} to {publish}?")

        if not approved:
            print("\n[4/4] Not published — review declined")
        elif publish == "gdocs":
            from gdocs_publish import create_google_doc
            print("\n[4/4] Publishing to Google Docs...")
            result["url"] = create_google_doc(ticket["title"], document, public=public)
        elif publish == "confluence":
            from confluence_publish import publish_to_confluence
            print("\n[4/4] Publishing to Confluence...")
            result["url"] = publish_to_confluence(ticket["title"], document)

    result.update(ok=True, chars=len(document), usage=usage, cost=cost,
                  seconds=time.time() - started)
    return result


def print_summary(results: list):
    """The numbers that go in the README and the interview reference."""
    print(f"\n{'='*60}")
    print("  RUN SUMMARY")
    print(f"{'='*60}")

    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]

    for r in results:
        status = "OK  " if r["ok"] else "FAIL"
        tokens = r["usage"].get("total_tokens")
        line = f"  {status}  {r['ticket']:<12} {r['seconds']:>6.1f}s"
        if tokens:
            line += f"  {tokens:>6} tok"
        if r["cost"] is not None:
            line += f"  ${r['cost']:.6f}"
        if r["url"]:
            line += f"  -> {r['url']}"
        print(line)

    if ok:
        total_time = sum(r["seconds"] for r in ok)
        costs = [r["cost"] for r in ok if r["cost"] is not None]
        toks = [r["usage"].get("total_tokens") for r in ok
                if r["usage"].get("total_tokens")]
        print(f"\n  {len(ok)} succeeded, {len(failed)} failed")
        print(f"  Total {total_time:.1f}s · avg {total_time/len(ok):.1f}s per document")
        if toks:
            print(f"  Avg tokens: {sum(toks)//len(toks)}")
        if costs:
            print(f"  Total cost: ${sum(costs):.6f} · avg ${sum(costs)/len(costs):.6f} per document")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Jira ticket(s) → LLM → technical document")
    ap.add_argument("tickets", nargs="*", default=["DEMO-1"],
                    help="One or more Jira issue keys, e.g. DEMO-1 DEMO-2")
    ap.add_argument("--llm", choices=["openrouter", "ollama"], default="openrouter")
    ap.add_argument("--publish", choices=["none", "gdocs", "confluence"], default="none")
    ap.add_argument("--public", action="store_true",
                    help="with --publish gdocs: share as anyone-with-link")
    ap.add_argument("--review", action="store_true",
                    help="print each document and ask before publishing")
    ap.add_argument("--price-in", type=float, default=DEFAULT_PRICE_IN,
                    help="USD per 1M input tokens, for cost reporting")
    ap.add_argument("--price-out", type=float, default=DEFAULT_PRICE_OUT,
                    help="USD per 1M output tokens, for cost reporting")
    args = ap.parse_args()

    tickets = args.tickets or ["DEMO-1"]
    print(f"\nGenAI Document Writing Assistant — {len(tickets)} ticket(s)")

    results = [
        process_ticket(t, llm=args.llm, publish=args.publish, public=args.public,
                       review=args.review, price_in=args.price_in,
                       price_out=args.price_out)
        for t in tickets
    ]
    print_summary(results)
