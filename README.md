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
│   └── publishers/              # Google Docs, Confluence, markdown conversion
├── tests/                       # pytest suite — no network or credentials needed
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
```

`python main.py ...` and `python -m docgen ...` accept the same arguments.

Exit codes: `0` every ticket succeeded · `1` at least one ticket failed · `2` setup problem
(missing env vars, bad credentials, invalid ticket key).

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

## Tests

```bash
pytest
```

## How It Works

### 1. Jira Integration
Connects to Jira REST API v3 using Basic Auth (email + API token).
Fetches title, description, status, labels. The description arrives as Atlassian Document Format
(a JSON tree) and is flattened to plain text before it reaches the model.

### 2. LLM Document Generation
Builds a dynamic prompt using ticket data and sends it to:
- **OpenRouter** (Gemini 2.0 Flash) — cloud, high quality
- **Ollama** (Qwen) — local, free, private

LLM generates a structured technical document with:
- Overview
- Technical Details
- Implementation Steps
- Acceptance Criteria
- Known Limitations

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
