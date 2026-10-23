# Evaluation harness

`docgen eval` runs a set of fixture tickets through generation and scores
every document against the ticket it came from.

```bash
docgen eval                                   # built-in fixtures, default OpenRouter model
docgen eval --compare                         # same fixtures through Gemini (OpenRouter) and Ollama, side by side
docgen eval --models openrouter:meta-llama/llama-3.1-8b-instruct ollama:llama3.1
docgen eval --judge openrouter                # add an LLM-judged faithfulness score
docgen eval --only login-jwt --details        # one fixture, with the reasons behind each score
docgen eval --output report.json --save-docs eval-docs/
docgen eval --min-score 0.85                  # exit 1 if any model averages below 0.85
```

Eval needs only the LLM settings (`OPENROUTER_API_KEY`, or a running Ollama).
The fixtures replace Jira, so no Jira credentials are involved.

## Metrics

Every score is in [0, 1] and comes with the checks behind it (`--details`).
**Overall** is the mean of the three.

### Faithfulness — no claims absent from the ticket

*Default: entity grounding (free, deterministic).* LLM fabrications are
mostly *specifics*: a number, an endpoint path, a product name, a config
key, a URL. The scorer pulls every such specific out of the document and
looks it up in the ticket.

    score = specifics found in the ticket / distinct specifics in the document

These are allowed: generic vocabulary the prompt permits as standard practice
(HTTP, JSON, common status codes), template section names, and placeholders
like `<user-id>`. The template's `unknowns` section is skipped, because
"the timeout is not specified" isn't a claim. Example payloads inside fenced
code blocks are skipped too, apart from paths and URLs.

Limitation: it can't see an unsupported claim made in ordinary words ("the
service retries failed requests"). That's what the judge is for.

*With `--judge BACKEND[:MODEL]`:* a second model lists the document's claims
and marks each as supported by the ticket or not. Its score replaces the
heuristic as `faithfulness`, and the heuristic is kept as
`faithfulness_heuristic` in the JSON report. The judge's tokens and cost are
reported separately from generation. If the judge fails or returns
unparseable output, the heuristic score is used and the failure is recorded.

### Completeness — required sections present and non-trivial

Each required template section scores 1 if present and non-trivial, 0.5 if
present but trivial (under `min_words`, or placeholder text like "TBD"), and
0 if missing. Headings match the section title or its aliases, ignoring
numbering, emphasis and case.

### Structure — follows the template's output schema

The fraction of these checks that pass:

- no preamble before the first section ("Sure! Here's your document…")
- template sections at the template's heading level
- sections in template order
- no duplicated sections
- code fences closed
- each section's `format` satisfied (list / ordered list / table / code / prose)
- `max_words` and `allow_extra_sections`, when the template sets them

## Tokens and cost

Every run records prompt, completion and total tokens. Cost is what the
provider reports (OpenRouter returns the billed cost; Ollama is local and
counts as $0). If no cost is reported, it's estimated from `--price-in` and
`--price-out` (USD per 1M tokens).

## Fixtures

`src/docgen/eval/fixtures/tickets.yaml` has one realistic ticket per built-in
template. Use your own file with `--fixtures`:

```yaml
- id: my-case
  template: runbook
  ticket:
    key: OPS-12
    title: Rotate the API signing key
    description: |
      ...
    labels: [ops]
```

## Output

Tables are written to stdout and logs to stderr, so `docgen eval > scores.txt`
captures just the report. `--output report.json` writes everything: each
record's scores, checks, notes, tokens, cost, timing and the generated
document.
