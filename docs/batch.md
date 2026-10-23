# Batch runs, incremental updates and ticket context

## Batch: JQL and concurrency

```bash
docgen --jql "project = DEMO AND status = Done" --publish gdocs
docgen --jql "sprint in openSprints()" -j 8 --max-results 200
docgen DEMO-2 DEMO-5 --jql "labels = release-2.4"     # explicit keys and a query, merged
```

- `--jql` uses Jira's `/rest/api/3/search/jql` endpoint, which pages with
  `nextPageToken`. The older `/rest/api/3/search` has been retired. An invalid
  query stops the run before anything is generated (exit 2), with Jira's error
  message.
- `--max-results` caps how many matches are taken (default 100).
- `-j/--concurrency` sets how many tickets run in parallel (default 4). Each
  thread has its own Jira session and Google API client, because neither is
  safe to share between threads. Rate limits are still handled per call by the
  retry decorator.
- One failing ticket doesn't stop the batch. A configuration or
  authentication failure does, since it would fail the same way for every
  ticket: queued tickets are dropped, running ones stop at their next step,
  and the run exits 2.
- **Progress.** On a terminal you get a live status line:

      docgen ▕██████████░░░░░░░░░░▏ 6/12  ✓5 ✗1  =2 unchanged  running DEMO-7, DEMO-8  0:41 ETA 0:39

  Log lines still print above it. Off a terminal (CI, the server, pipes) a
  `progress` log line is written per ticket instead. `--no-progress` turns
  the bar off.

## Incremental updates

docgen records every document it makes in a SQLite store (`.docgen/docgen.db`,
or `--store` / `DOCGEN_STORE`). Each row maps *(ticket, template, target)* to
the Google Doc id, Confluence page id or local file, plus the published
Markdown and a fingerprint of everything the model saw: ticket, context,
template, model and context level.

On the next run, for each ticket:

| Situation | What happens |
|---|---|
| Nothing the model would see has changed | Skipped: no tokens, no API calls (`unchanged`) |
| Ticket changed | Regenerated, diffed against the stored version, and the **existing** doc/page updated in place (`updated`) |
| Ticket changed but the model wrote an identical document | Not republished; the new fingerprint is remembered |
| The Doc was deleted or trashed, or the page removed | A new one is created and the mapping updated |
| `--force` | Regenerated and republished regardless |
| `--no-store` | Old behaviour: a new document every run |

**Google Docs update in place.** One `batchUpdate`, pinned with
`requiredRevisionId` to the revision read just before, does all of it: delete
the old body, insert the new text, reset inherited paragraph, bullet and text
styles, then restyle. The doc keeps its URL, sharing, comments and version
history. If the ticket title changed, the file is renamed.

**Confluence update in place.** The page is `PUT` with `version.number`
set to current + 1, so it shows up in the page history. Confluence rejects a
stale version, so a retry after a lost response can't apply the update twice.

**What changed.** Every update logs a summary such as
`+12/-5 lines; added: Rollback; changed: Procedure`. `--show-diff` also prints
the unified diff (coloured on a terminal). The server returns the same summary
and diff in each job's results.

```bash
docgen docs                 # what has been published where
docgen docs DEMO-2 --json
```

## Ticket context (`--context`)

| Level | Sent to the model |
|---|---|
| `minimal` | Title, description, status, labels |
| `standard` (default) | Plus issue type and priority, the last 10 comments, linked issues (relation, summary, status), subtasks, attachment names |
| `full` | Plus up to 50 comments, the descriptions of up to 10 linked issues and subtasks, and image attachments downloaded and embedded in the document |

The context block is capped (15k characters at `standard`, 40k at `full`). The
oldest comments are dropped first. Anything you can't see (a linked issue in a
restricted project, an attachment that fails to download) is logged and
skipped rather than failing the ticket.

The faithfulness scorer and the LLM judge treat the context as source
material, so a document that quotes a comment isn't penalised.

### Images

At `full`, PNG, JPEG and GIF attachments (up to 10, 10 MB each) are downloaded
to `.docgen/attachments/<TICKET>/` and offered to the model by name. It places
them with `![caption](attachment:name.png)`. References to images that don't
exist are dropped, and images the model didn't place are added under an
"Attachments" heading.

| Output | How images get there |
|---|---|
| Local Markdown | Copied to `<TICKET>_attachments/` next to the file, with relative links |
| Google Docs | Uploaded to Drive and shared by link only long enough to be inserted (Docs keeps its own copy), then the Drive file is deleted. If Google can't fetch it, a `[image: name]` placeholder is left and the publish carries on. |
| Confluence | Uploaded as page attachments and embedded with `<ac:image>` |

The Google Docs API can only insert images from a URL it can fetch without
credentials. Docs therefore has a few seconds where the image file is readable
by anyone with its (unguessable) link. Use `--context standard` if that's
not acceptable.
