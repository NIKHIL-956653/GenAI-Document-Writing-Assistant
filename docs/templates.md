# Document templates

A template decides what kind of document `docgen` writes: its prompt, its
sections, and the output schema the result is checked against. Templates are
YAML files, so adding a document type needs no code.

```bash
docgen templates                        # list what's available
docgen DEMO-2 --template runbook        # use one (short: -t runbook)
```

| Template | Document |
|---|---|
| `technical` (default) | General technical write-up: Overview, Technical Details, Implementation Steps, Acceptance Criteria, Known Limitations |
| `prd` | Product requirements: problem, goals, non-goals, user stories, requirements, success metrics |
| `runbook` | On-call procedure: prerequisites, numbered steps, verification, rollback, escalation |
| `release_notes` | Customer-facing notes: summary, what's new, breaking changes, upgrade notes |
| `api_docs` | API reference: auth, endpoint table, request/response examples, errors |
| `postmortem` | Blameless incident review: impact, timeline, root cause, resolution, action items |

## Where templates are found

Highest priority first — a file with the same name overrides a lower one:

1. `--templates-dir DIR`
2. the directory in `DOCGEN_TEMPLATES_DIR`
3. `./templates/` in the directory you run `docgen` from
4. the built-ins in `src/docgen/templates/`

The file name (without `.yaml`) is the template name.

## Schema

```yaml
title: Architecture Decision Record          # required — shown to the model ("Write a <title> ...")
description: One decision and its consequences.
system_prompt: |                              # required — persona and rules
  You are a staff engineer writing an ADR.
instructions: |                               # optional — appended to the user message
  Keep it under one page.
sections:                                     # required — in the order they must appear
  - title: Context
    guidance: The forces at play.             # shown to the model next to the heading
    format: prose                             # any | prose | list | ordered_list | table | code, or a list of them
    min_words: 30                             # below this a present section is "trivial" (0 = no minimum)
    required: true                            # optional sections are offered, never penalised when absent
    aliases: [Background]                     # other headings that count as this section
  - title: Decision
  - title: Consequences
    format: [list, prose]
  - title: Open Questions
    unknowns: true                            # where the model must park what the ticket doesn't say
output:
  heading_level: 2                            # sections are `##` headings
  allow_extra_sections: true                  # false = headings not in `sections` fail structure
  max_words: 1200                             # optional length cap
```

Optional `user_prompt` replaces the generated user message entirely. It takes
`$`-placeholders — `$key $title $description $status $labels $sections
$instructions` — so braces in examples (JSON) stay literal.

## The faithfulness rule

Every template's system prompt gets the same rule appended: only state facts
from the ticket or standard engineering practice, and put anything
unspecified in the `unknowns: true` section. Give every template one — the
evaluation harness skips that section when checking for invented claims.

## How the schema is used

The same fields drive generation *and* evaluation (`docgen eval`):

- `sections`, `format`, `guidance` → the section list in the prompt
- `required`, `min_words` → the **completeness** score
- `format`, `heading_level`, order, `allow_extra_sections`, `max_words` → the **structure** score
- `unknowns` → excluded from **faithfulness** checking

A template that fails validation (bad YAML, duplicate section titles, two
`unknowns` sections, an unknown `format`) is reported with the file and the
field at fault, before any API call.
