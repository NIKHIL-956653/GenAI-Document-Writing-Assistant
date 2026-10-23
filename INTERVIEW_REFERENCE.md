# GenAI Document Writing Assistant — Interview Reference

Personal prep notes. Everything here is true of the code as of this commit; nothing is padded.

---

## 1. The 30-second pitch

> "Engineers write tickets, not documentation. This tool turns a Jira ticket into a
> publication-ready technical document and publishes it to Google Docs or Confluence with one
> command. Under the hood it's an integration project: three external APIs with three different
> authentication models, plus an LLM layer that's swappable between a cloud model and a fully
> local one for data-sensitive environments."

If they ask "why did you build it": documentation debt is universal, and the *input* already exists
in every company's tracker. The LLM part is the easy part — the value is in the plumbing being reliable.

---

## 2. Architecture

```
Jira Cloud ──REST v3──▶ docgen.jira ──Ticket──▶ docgen.llm ──GeneratedDocument──▶ docgen.pipeline
   (Basic auth:            (ADF → text)          (OpenRouter | Ollama)                 │
    email + API token)                                                                 ├─▶ <ticket>_document.md
                                                                                       ├─▶ publishers.gdocs      (OAuth 2.0, Docs + Drive API)
                                                                                       └─▶ publishers.confluence (same Atlassian token, storage format)
```

Installable package (`src/docgen/`, `pip install -e .`, console entry point `docgen`). Every
outbound call goes through `docgen.http.send()` (always with a timeout, failures → typed errors)
and one `@retry` decorator.

| Module | Responsibility | The one detail to know cold |
|---|---|---|
| `docgen.jira` | GET `/rest/api/3/issue/{key}`; normalise to a `Ticket` (Pydantic) | v3 returns `description` as **ADF** (a JSON tree), not text — `adf_to_text()` walks it |
| `docgen.llm` | `build_messages()` (one prompt) + two backends: OpenRouter, Ollama | System prompt pins the 5-section structure and tells the model to put unknowns under *Known Limitations* instead of inventing them |
| `docgen.publishers.gdocs` | OAuth installed-app flow → `documents.create` → one styled `batchUpdate` → optional Drive share | Write is pinned to the create's `revisionId`, so a retried write can't insert the text twice |
| `docgen.publishers.confluence` | POST `/wiki/rest/api/content` with storage-format XHTML | Markdown → XHTML via `markdown_to_storage()` |
| `docgen.retry` / `docgen.errors` | `@retry` decorator; typed error hierarchy | Rate limits (honour Retry-After, longer base) are handled differently from transient 5xx/timeouts (full-jitter backoff); auth/404/400 never retry |
| `docgen.config` | Pydantic settings per integration; `preflight()` | Reports *every* missing env var before the first API call or token spent |
| `docgen.cli` | `docgen DEMO-2 --llm ollama --publish gdocs [--public]` | Saves locally *before* publishing so a publish failure never loses the generated doc |

---

## 3. Design decisions (say these unprompted — they show judgement)

**One prompt, two backends.** `build_messages()` is shared; only the HTTP transport differs. Cloud
(Gemini Flash-Lite via OpenRouter) for quality and speed; Ollama (Qwen 2.5) for data that must not
leave the machine. In the UAE market "ticket content stays on-prem" is a real requirement — this is
the answer to it, not an afterthought.

**Structured system prompt.** Five fixed sections → every document has the same shape, so a reviewer
knows where to look and downstream tooling (a template, a checker) can rely on it. The
anti-hallucination line ("if the ticket doesn't specify, say so under Known Limitations") is the
cheapest guardrail with the biggest effect on trust.

**OAuth 2.0 user consent for Google, not a service account.** A service account owns whatever it
creates, in *its* Drive, invisible to the user unless shared. The installed-app OAuth flow makes the
document appear in the engineer's own Drive under their name. Refresh token is persisted
(`token.pickle`) so consent happens once. Trade-off: not headless — fine for a CLI tool, wrong for a server.

**Save-then-publish.** Local markdown is written before any network publish. Publishing is the
flakiest step (token expiry, quota, network) and must never cost you the generated content.

**Private by default.** Docs are created unshared; `--public` is an explicit flag. The first version
made every doc "anyone with the link" — wrong default for internal documentation.

---

## 4. Authentication — the question you WILL get

| Service | Method | Where the secret lives | Failure mode you handled |
|---|---|---|---|
| Jira / Confluence | HTTP Basic: email + **API token** (never the password) over HTTPS | `.env` | 401 → clear error, empty dict, pipeline stops |
| OpenRouter | Bearer key | `.env` | non-200 → logged with response body, empty string returned |
| Google Docs/Drive | OAuth 2.0 authorization-code flow (installed app), scopes `documents` + `drive`; short-lived access token + long-lived refresh token | `credentials.json` (client secret) + `token.pickle` (tokens) — both git-ignored | expired access token → refreshed silently; no refresh token → browser consent re-runs |

Why an API token instead of a password: revocable per-integration, scoped to the account, doesn't
expose the login credential, and Atlassian is deprecating password basic-auth anyway.

---

## 5. Things that actually broke (good "tell me about a bug" answers)

1. **The prompt was receiving a Python dict.** Jira's v3 API returns rich text as Atlassian Document
   Format. The first version passed the raw JSON tree into the prompt as `str(dict)`. The model
   coped — LLMs are forgiving — which is exactly why it went unnoticed. Fix: a recursive walker that
   flattens paragraphs, headings, lists and code blocks to text. Lesson: *inspect what the model
   actually receives, not what you think it receives.*
2. **`.gitignore` that ignored nothing.** Created with PowerShell `echo ... > .gitignore`, which
   writes UTF-16 by default. Git read the file as binary garbage and applied no patterns — the OAuth
   client secret, refresh token and `.env` were one `git add -A` away from GitHub. Caught by running
   `git check-ignore credentials.json`. Lesson: verify security controls, don't assume them.
3. **Confluence returns 200 *or* 201** for a created page depending on the endpoint version; the
   check was `!= 200`. Small, but it's the kind of thing that only shows up in production.
4. **`requirements.txt` listed libraries the code never imported** (LangChain, Streamlit) and was
   missing the Google client libraries it *did* need. Now it matches the imports exactly.

---

## 6. Likely questions and strong answers

**"Walk me through what happens when I run it."**
CLI parses the ticket key → Jira GET → ADF flattened to text → messages built (system + user) →
POST to OpenRouter or Ollama → markdown saved locally as `<KEY>_document.md` → optional publish:
Docs (create, insert text at index 1, optionally share) or Confluence (storage-format POST).

**"How do you stop the model inventing requirements?"**
Three layers, in order of cost: the system prompt instructs it to declare gaps under *Known
Limitations*; the ticket fields are the only facts in the prompt (no external retrieval to
hallucinate from); and the document is saved locally for human review before anything is published.
Roadmap: a section-presence check and an LLM-as-judge rubric that flags claims not traceable to a ticket field.

**"Why not LangChain?"**
Two HTTP calls don't need a framework. The abstraction that matters here is `build_messages()` —
one prompt contract shared by both backends. Adding a third backend is one function.

**"What would production look like?"**
Trigger: Jira webhook on status transition (e.g. *Ready for Docs*) instead of a CLI. Worker: a queue
with retries and exponential backoff on 429/5xx. Idempotency key = ticket key + updated-timestamp
so re-runs update rather than duplicate. Secrets in a manager (not `.env`), service-account or
domain-wide delegation for Google so it can run headless, structured logging with the ticket key on
every line, and a review step (draft → approve → publish) because published docs carry the company's name.

**"How would you evaluate document quality?"**
Deterministic checks first (all five sections present, no empty section, length bounds). Then a
small golden set of tickets with human-rated docs, re-scored on every prompt change with an
LLM-as-judge rubric (accuracy to ticket, completeness, clarity). Prompt changes without a regression
number are guesses.

**"Data privacy — you're sending our tickets to a third-party model."**
That's why the Ollama path exists: identical prompt, model runs on the engineer's machine, nothing
leaves. For the cloud path: OpenRouter is a router — pick a provider with a no-training policy, and
redact obvious PII from the description before the call (roadmap).

**"What's the cost per document?"**
One call, roughly a short prompt in and a ~1–2 page document out, on a Flash-Lite-class model —
fractions of a cent. *(Measure it: the OpenRouter dashboard shows tokens per request; quote your
real number.)*

**"How does the Google token refresh work?"**
`token.json` holds the authorized-user credentials (JSON, not pickle). On start: load → if expired
and a refresh token exists, `creds.refresh()` gets a new access token silently → else run the
local-server consent flow → persist. The user consents once. If the refresh token itself has been
revoked or expired (7 days for apps in "Testing" status), the `RefreshError` becomes an `AuthError`
telling the user to delete `token.json` and sign in again.

**"Scale it to 1,000 tickets a day."**
Batch by project, run generation concurrently (it's I/O-bound), respect each API's rate limits
with backoff, cache the ticket snapshot so a retry doesn't re-hit Jira, and move publishing to a
service identity. The LLM call is the bottleneck by latency, the Docs API by quota.

**"What would you do differently?"**
Start with the webhook trigger and the review step — the CLI was right for a prototype, but the
product is "docs appear when tickets move," not "run a script."

---

## 7. Honest limitations (say them before they're found)

- Markdown styling covers headings, lists, bold, inline/fenced code — not tables or links in Google Docs.
- Retries can re-send a non-idempotent call whose response was lost (an OpenRouter generation is
  paid twice; a Confluence page create returns "already exists").
- OAuth installed-app flow means it cannot run unattended on a server.
- No PII redaction before the cloud LLM call.
- Batch mode is sequential; no concurrency yet.

---

## 8. Two-minute demo script

1. Show the Jira ticket in the browser (title, a description with a bullet list — proves ADF handling).
2. `docgen DEMO-2` → narrate the four steps as they print.
3. Open the generated `DEMO-2_document.md` — point at the five sections and at *Known Limitations* naming something the ticket didn't specify.
4. `docgen DEMO-2 --publish gdocs` → the Doc opens in your Drive, private.
5. If asked about privacy: `docgen DEMO-2 --llm ollama` — same document, no internet call.

---

## 9. Numbers to measure before the interview (fill in, then quote confidently)

- Time for a full run, cloud: ____ s · local Ollama on CPU: ____ s
- Tokens in / out for a typical ticket (OpenRouter dashboard): ____ / ____
- Cost per document at current pricing: $____
- Lines of code: see `src/docgen/` (package) and `tests/`
