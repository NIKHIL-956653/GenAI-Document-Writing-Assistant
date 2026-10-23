# GenAI Document Writing Assistant

An AI-powered technical documentation assistant that automatically generates 
and publishes product documentation from Jira tickets using LLMs.

## What It Does
Jira Ticket (DEMO-1)
↓
Fetches ticket data via Jira REST API
↓
Feeds ticket into LLM (Gemini / Ollama)
↓
AI generates full technical document
↓
Publishes to Google Docs automatically

## Tech Stack

- **Python** — core language
- **Jira REST API** — ticket data source
- **OpenRouter (Gemini 2.0 Flash)** — cloud LLM
- **Ollama (Qwen)** — local LLM, same prompt, nothing leaves the machine
- **Google Docs API** — document publishing
- **Google Drive API** — file management
- **Pydantic / pydantic-settings** — typed config and data models, `.env` loading
- **PyYAML** — document templates and eval fixtures
- **FastAPI / uvicorn** — HTTP API and Jira webhook (`docgen[server]`)
- **SQLite** — incremental ticket → document store and job state
- **Docker / Compose** — API image, plus Ollama for fully local operation
- **pytest, ruff, mypy, GitHub Actions** — tests, lint, types, CI

## Project Structure

```
GenAI-Document-Writing-Assistant/
├── pyproject.toml               # Package metadata, dependencies, `docgen` entry point
├── src/docgen/
│   ├── cli.py                   # `docgen` command (argparse)
│   ├── pipeline.py              # fetch → generate → save → review → publish, batch + summary
│   ├── config.py                # Pydantic settings per integration + preflight checks
│   ├── models.py                # Ticket, TokenUsage, GeneratedDocument, RunResult
│   ├── errors.py                # Typed errors (ConfigError, AuthError, RateLimitError, ...)
│   ├── retry.py                 # @retry decorator: backoff + jitter, rate-limit aware
│   ├── http.py                  # requests wrapper: timeouts, status → typed error
│   ├── logging_setup.py         # Structured logging (text or JSON)
│   ├── jira/                    # Jira REST v3 client, ADF → text
│   ├── llm/                     # Prompt contract + OpenRouter / Ollama backends
│   ├── templates/               # Document types as YAML (prd, runbook, ...) + loader
│   ├── eval/                    # Scorers, LLM judge, fixture tickets, `docgen eval`
│   ├── publishers/              # Google Docs, Confluence (create or update in place)
│   ├── store.py                 # SQLite: ticket → document mapping, server jobs
│   ├── diff.py                  # What changed between versions
│   ├── assets.py                # Ticket images in documents
│   ├── progress.py              # Batch progress bar
│   └── server.py                # FastAPI app + Jira webhook
├── docs/                        # templates, evaluation, batch, server guides
├── Dockerfile, docker-compose.yml  # API image; API + Ollama stack
├── tests/                       # pytest suite — no network or credentials needed
├── .github/workflows/ci.yml     # ruff + mypy + pytest/coverage on every PR
└── main.py                      # Back-compat shim for `python main.py ...`
```

## Setup

### 1. Clone and install
```bash
git clone https://github.com/NIKHIL-956653/GenAI-Document-Writing-Assistant.git
cd GenAI-Document-Writing-Assistant
python -m venv venv
source venv/bin/activate            # Windows: .\venv\Scripts\Activate.ps1
pip install -e ".[dev]"             # installs the `docgen` command
```

### 2. Create a `.env` file (in the directory you run `docgen` from)
```
JIRA_DOMAIN=yourname.atlassian.net
JIRA_EMAIL=your@email.com
JIRA_TOKEN=your_jira_api_token
OPENROUTER_API_KEY=your_openrouter_key
```

Optional settings (defaults shown):

| Variable | Default | Used by |
|---|---|---|
| `OPENROUTER_MODEL` | `google/gemini-2.0-flash-lite-001` | `--llm openrouter` |
| `OPENROUTER_TIMEOUT` | `120` (seconds) | `--llm openrouter` |
| `OLLAMA_HOST` | `http://localhost:11434` | `--llm ollama` |
| `OLLAMA_MODEL` | `qwen2.5:latest` | `--llm ollama` |
| `OLLAMA_TIMEOUT` | `600` | `--llm ollama` |
| `JIRA_TIMEOUT` | `30` | always |
| `CONFLUENCE_SPACE_KEY` | `KAN` | `--publish confluence` |
| `CONFLUENCE_TIMEOUT` | `60` | `--publish confluence` |
| `GOOGLE_CREDENTIALS_FILE` | `credentials.json` | `--publish gdocs` |
| `GOOGLE_TOKEN_FILE` | `token.json` | `--publish gdocs` |
| `GOOGLE_TIMEOUT` | `60` | `--publish gdocs` |

Only the variables a run needs are required — `--llm ollama` doesn't need an OpenRouter key.
`docgen` checks all of them before making any API call and lists everything that is missing.

### 3. Add Google credentials (only for `--publish gdocs`)
- Go to Google Cloud Console
- Enable Google Docs API and Google Drive API
- Create OAuth 2.0 credentials of type **Desktop app**
- Download as `credentials.json` and place it next to your `.env`

The first publish opens a browser for consent and saves `token.json`. (Earlier versions saved
`token.pickle`; that file is no longer read — you'll be asked to consent once more.)

## Run

```bash
docgen DEMO-2                          # Jira → LLM → DEMO-2_document.md
docgen DEMO-2 --llm ollama             # same, fully local model
docgen DEMO-2 --publish gdocs          # + create a private Google Doc
docgen DEMO-2 --publish gdocs --public # + share as anyone-with-link
docgen DEMO-2 --publish confluence     # + create a Confluence page
docgen DEMO-1 DEMO-2 DEMO-3 --review   # batch; show each doc and ask before publishing
docgen DEMO-2 --log-format json        # one JSON log object per line (stderr)
docgen DEMO-2 --template runbook       # pick the document type (see below)
docgen --jql "project = DEMO AND status = Done" -j 8 --publish gdocs   # batch, 8 at a time
docgen DEMO-2 --publish gdocs --show-diff   # re-run: updates the same Doc, shows what changed
docgen DEMO-2 --context full           # + all comments, related issues, embedded images
docgen docs                            # which ticket went to which document
```

`python main.py ...` and `python -m docgen ...` accept the same arguments.

Exit codes: `0` every ticket succeeded · `1` at least one ticket failed · `2` setup problem
(missing env vars, bad credentials, invalid ticket key or JQL).

## Batch, incremental updates, context

- **Batch.** `--jql` takes every matching issue, and `-j N` processes N
  tickets at a time. A live progress bar shows done/failed/unchanged counts,
  what's running, and an ETA.
- **Incremental.** A local SQLite store (`.docgen/docgen.db`) maps each
  ticket + template + target to its Google Doc, Confluence page or file. A
  re-run skips tickets that haven't changed, which costs no tokens. A changed
  ticket **updates the existing document in place** (same URL, sharing and
  comments) and logs what changed; `--show-diff` prints the diff.
- **Context.** `--context minimal | standard | full` controls how much of the
  ticket's surroundings the model sees. `standard` (the default) adds
  comments, linked issues and subtasks. `full` adds their descriptions and
  embeds the ticket's images in the local file, Google Doc or Confluence page.

Details: [docs/batch.md](docs/batch.md).

## HTTP API, Jira webhook, Docker

```bash
pip install -e ".[server]" && docgen serve     # http://127.0.0.1:8000/docs
docker compose up -d                           # API + Ollama: fully local LLM
```

- `POST /generate` and `POST /publish` queue jobs and return 202.
  `GET /status/{id}` reports per-ticket results, including what changed.
- `POST /webhooks/jira` generates a document automatically when an issue
  moves into a configured status (`DOCGEN_WEBHOOK_STATUSES`). Requests are
  HMAC-verified and Jira's retries are de-duplicated.
- Bearer-token auth (`DOCGEN_API_TOKEN`). The Docker image runs as a non-root
  user, and compose adds an Ollama service that pulls the model on first
  start.

Details: [docs/server.md](docs/server.md). All settings: [.env.example](.env.example).

## Document templates

`--template` picks the document type: `technical` (the default), `prd`,
`runbook`, `release_notes`, `api_docs` or `postmortem`. Each template is a
YAML file that defines its own prompt, its sections, and the output schema
the result is checked against. To add a type, drop a YAML file into
`./templates/` (or `--templates-dir` / `DOCGEN_TEMPLATES_DIR`); no code
changes needed. `docgen templates` lists what's available. Schema and
examples: [docs/templates.md](docs/templates.md).

```bash
docgen DEMO-7 --template postmortem --publish confluence
```

## Evaluation

`docgen eval` runs fixture tickets through one or more models and scores every
document against the ticket it was written from:

- **Faithfulness**: no claims absent from the ticket. Every specific in the
  document (numbers, endpoints, product names, config keys, URLs) is checked
  against the ticket. `--judge` adds an LLM-as-judge score.
- **Completeness**: every required template section is present and has real content.
- **Structure**: the document follows the template's schema (heading levels,
  order, list/table/code formats, no preamble).

```bash
docgen eval --compare --details        # Gemini (OpenRouter) vs Ollama, side by side
docgen eval --judge openrouter --output report.json
docgen eval --min-score 0.85           # CI gate
```

Tokens and cost are tracked per run and per model. Details:
[docs/evaluation.md](docs/evaluation.md).

## Reliability

- **Timeouts on every call** — Jira, OpenRouter, Ollama, Confluence and Google all have connect and
  read timeouts (configurable above).
- **One retry decorator** (`docgen.retry.retry`) for every API call:
  - *Rate limits* (HTTP 429, Google's 403 `rateLimitExceeded`, OpenRouter upstream 429s) wait as
    long as the server's `Retry-After` asks, or back off from a longer base when it doesn't say.
    A requested wait over 2 minutes fails fast instead of stalling a batch.
  - *Transient failures* (5xx, timeouts, dropped connections) use exponential backoff with full
    jitter, so batch runs don't retry in lockstep.
  - *Everything else* (bad credentials, 404, 400) fails immediately — retrying can't fix it.
- **Clear setup errors** — missing env vars are named; an expired or revoked Google token says to
  delete `token.json` and sign in again; an invalid ticket key is rejected before any request; a
  Jira 404 explains it may be a permissions issue; Ollama not running says `ollama serve`.
- **A failing ticket doesn't stop a batch**; config/auth failures abort it, since they'd fail
  identically for every ticket.

## Tests and CI

```bash
pytest --cov            # 368 tests, no network or credentials needed; fails under 95% coverage
ruff check .
mypy
```

Jira, OpenRouter, Ollama, Confluence and the Google APIs are faked at the
transport layer, so the tests run the real retry, error-classification and
CLI code. [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs ruff,
mypy, and pytest with coverage (Python 3.10–3.13), plus a Docker build and smoke test, on every
pull request.
The coverage table is posted to the job summary and as a PR comment.

## How It Works

### 1. Jira Integration
Connects to Jira REST API v3 using Basic Auth (email + API token).
Fetches title, description, status and labels, plus comments, linked issues, subtasks and
attachments, depending on `--context`. The description arrives as Atlassian Document Format
(a JSON tree) and is flattened to plain text before it reaches the model.

### 2. LLM Document Generation
Builds the prompt from the ticket and the selected template and sends it to:
- **OpenRouter** (Gemini 2.0 Flash) — cloud, high quality
- **Ollama** (Qwen) — local, free, private

The template decides the sections. The default `technical` template produces
Overview, Technical Details, Implementation Steps, Acceptance Criteria and
Known Limitations. Every template's prompt tells the model to put anything
the ticket doesn't specify into its "unknowns" section instead of inventing it.

### 3. Google Docs Publishing
Uses OAuth 2.0 (installed-app flow, consent once, refresh token persisted to `token.json`).
Creates a new Google Doc and inserts the generated content with headings, lists, bold and code styled.
Private by default; `--public` shares it as anyone-with-link.

## Example Output

Input ticket:
DEMO-1: User Authentication Feature
Description: Implement secure login with JWT tokens

Output: Full technical document published to Google Docs with
overview, implementation steps, acceptance criteria, and limitations.

## Results

- ✅ Successfully fetched tickets from Jira REST API
- ✅ Generated technical documents using Gemini 2.0 Flash
- ✅ Published documents to Google Docs automatically
- ✅ Supports both cloud (OpenRouter) and local (Ollama) LLMs

## Author

**Nikhil Chandra Sairam Tokala**  
AI/ML Engineer | GenAI Engineer | DevOps  
Dubai, UAE  
[LinkedIn](https://linkedin.com/in/nikhil-chandra-133ncsr200233) | 
[GitHub](https://github.com/NIKHIL-956653)
