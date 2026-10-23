# Running docgen as a service

```bash
pip install -e ".[server]"
docgen serve                       # http://127.0.0.1:8000, interactive docs at /docs
docker compose up -d               # API + Ollama, fully local LLM
```

## API

All endpoints except `/healthz` and the webhook require
`Authorization: Bearer $DOCGEN_API_TOKEN` when that variable is set. If it
isn't set, the API is open and logs a warning at startup. `docgen serve` binds
to 127.0.0.1 by default.

| Method | Path | |
|---|---|---|
| GET | `/healthz` | Liveness |
| GET | `/templates` | Available templates |
| POST | `/generate` | Generate documents (saved locally, not published). **202** + job |
| POST | `/publish` | Generate and publish to `gdocs` or `confluence`. **202** + job |
| GET | `/status/{job_id}` | Job state and per-ticket results, including what changed |
| GET | `/jobs?limit=20` | Recent jobs |
| GET | `/documents?ticket=DEMO-2&include_markdown=false` | The ticket→document store |
| POST | `/webhooks/jira` | Jira webhook (below) |

```bash
curl -X POST localhost:8000/publish -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"jql": "project = DEMO AND status = Done", "template": "release_notes", "target": "confluence"}'
# → {"job_id": "…", "status": "queued", "status_url": "/status/…"}
```

Request body (`/generate`; `/publish` adds `target` and `public`):

```json
{"tickets": ["DEMO-2"], "jql": null, "max_results": 50, "template": "technical",
 "llm": "openrouter", "model": null, "context": "standard", "force": false}
```

Fields left out fall back to the server's `DOCGEN_*` settings. Bad keys and
bodies get **422**, an unknown template **400**, and **503** if the server lacks
the settings the request needs (e.g. no `OPENROUTER_API_KEY`).

Jobs run on a worker pool (`DOCGEN_WORKERS`), and each job processes its
tickets with `DOCGEN_CONCURRENCY` threads. They use the same incremental store
as the CLI, so repeated requests only regenerate what changed. Job state lives
in SQLite: jobs still queued or running when the server stops are marked
`interrupted` at the next start.

## Jira webhook: generate on transition

1. Set `DOCGEN_WEBHOOK_SECRET`. The endpoint returns 503 until you do.
2. Choose the trigger statuses: `DOCGEN_WEBHOOK_STATUSES=Done,Ready for Release`
   (comma-separated, case-insensitive). Optionally set `DOCGEN_WEBHOOK_TEMPLATE`,
   `DOCGEN_WEBHOOK_PUBLISH=none|gdocs|confluence` and `DOCGEN_WEBHOOK_PUBLIC`.
3. In Jira (Settings → System → WebHooks), add
   `https://your-host/webhooks/jira` for *Issue updated*, with the same secret.
   Jira then signs each delivery with `X-Hub-Signature: sha256=…`, which docgen
   verifies.

   For a sender that can't sign, such as Jira Automation's "Send web request",
   pass the secret as an `X-Docgen-Webhook-Token` header or `?token=` instead.

docgen queues a job only when the event's changelog has a status change *into*
a configured status. Workflow post-function payloads (`transition.to_status`)
work too. Everything else gets 200 with `{"action": "ignored", "reason": …}`.
Jira retries deliveries, so each one is de-duplicated on
`X-Atlassian-Webhook-Identifier` (or the changelog id). A retry gets the
original job back instead of a second document.

## Google Docs from a server

The OAuth consent flow needs a browser, so the server never starts it. Run
`docgen auth google` once on a machine with a browser. That writes `token.json`.
Then make the file available to the server (`GOOGLE_TOKEN_FILE`; in Docker,
`docker compose cp token.json docgen:/data/token.json`). The token refreshes
itself after that. If it's revoked, publish jobs fail with a message saying to
repeat this step.

## Docker

- **`Dockerfile`:** a two-stage build that installs from wheels and runs as a
  non-root user. State lives in `/data`: the store, generated docs and Google
  tokens. It has a healthcheck on `/healthz`.
- **`docker-compose.yml`:**
  - **`ollama`:** the Ollama server, with models on a named volume. An NVIDIA
    GPU block is included, commented out.
  - **`ollama-pull`:** a one-shot service that pulls `OLLAMA_MODEL` (default
    `qwen2.5:latest`) before docgen starts.
  - **`docgen`:** the API with `DOCGEN_LLM=ollama`, on `127.0.0.1:8000`, with
    its data on the `docgen-data` volume.

  Nothing leaves the machine except calls to Jira, and to Google or
  Confluence when you publish there.

```bash
cp .env.example .env    # set JIRA_*, DOCGEN_API_TOKEN
docker compose up -d
docker compose logs -f docgen
```
