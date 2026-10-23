"""
HTTP API for the pipeline (`docgen serve`, or any ASGI server via create_app()).

    GET  /healthz              liveness, no auth
    GET  /templates            available document templates
    POST /generate             generate documents (saved locally, no publish)    → 202 + job
    POST /publish              generate and publish to Google Docs / Confluence  → 202 + job
    GET  /status/{job_id}      a job's state and per-ticket results (incl. what changed)
    GET  /jobs                 recent jobs
    GET  /documents            what has been published where (the incremental store)
    POST /webhooks/jira        Jira webhook: generate when an issue enters a configured status

Generation takes tens of seconds per ticket, so requests are queued as jobs
and run on a worker pool; job state lives in the SQLite store, so status
survives a restart. Every job runs the same incremental Pipeline as the CLI.

Auth: with DOCGEN_API_TOKEN set, every endpoint except /healthz and the
webhook needs `Authorization: Bearer <token>`. The webhook is verified with
DOCGEN_WEBHOOK_SECRET instead — Jira's `X-Hub-Signature: sha256=<hmac>`
header, or the secret itself as `X-Docgen-Webhook-Token` / `?token=` for
senders that can't sign (Jira Automation's "Send web request").
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from docgen import __version__, config
from docgen.config import DocgenSettings
from docgen.errors import ConfigError, DocgenError, InvalidTicketKeyError
from docgen.models import ContextLevel, normalise_ticket_key
from docgen.pipeline import Pipeline, PipelineOptions
from docgen.store import Job, Store, StoredDocument
from docgen.templates import available_templates, load_template

log = logging.getLogger(__name__)


# ── request / response models ────────────────────────────────

class GenerateRequest(BaseModel):
    tickets: list[str] = Field(default_factory=list, description="Jira issue keys")
    jql: str | None = Field(default=None, description="also process every issue matching this query")
    max_results: int = Field(default=50, ge=1, le=500)
    template: str | None = None
    llm: Literal["openrouter", "ollama"] | None = None
    model: str | None = None
    context: ContextLevel | None = None
    force: bool = False

    @field_validator("tickets")
    @classmethod
    def _keys(cls, values: list[str]) -> list[str]:
        try:
            return [normalise_ticket_key(v) for v in values]
        except InvalidTicketKeyError as exc:
            raise ValueError(exc.message) from None

    @model_validator(mode="after")
    def _something_to_do(self):
        if not self.tickets and not self.jql:
            raise ValueError("give `tickets`, `jql`, or both")
        return self


class PublishRequest(GenerateRequest):
    target: Literal["gdocs", "confluence"] = "gdocs"
    public: bool = False


class JobAccepted(BaseModel):
    job_id: str
    status: str
    status_url: str
    duplicate: bool = False


# ── job runner ───────────────────────────────────────────────

class JobRunner:
    def __init__(self, store: Store, settings: DocgenSettings, templates_dir: Path | None = None):
        self.store = store
        self.settings = settings
        self.templates_dir = templates_dir
        self.pool = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="docgen-job")

    def options(self, request: GenerateRequest) -> PipelineOptions:
        s = self.settings
        publish = request.target if isinstance(request, PublishRequest) else "none"
        return PipelineOptions(
            llm=request.llm or s.llm, model=request.model or s.model,
            template=request.template or s.template, templates_dir=self.templates_dir,
            context=request.context or ContextLevel(s.context), publish=publish,
            public=isinstance(request, PublishRequest) and request.public,
            force=request.force, concurrency=s.concurrency, output_dir=s.output_dir,
            interactive=False, progress=False)

    def validate(self, request: GenerateRequest) -> PipelineOptions:
        """Reject bad requests before queueing: unknown template (400), server misconfigured (503)."""
        options = self.options(request)
        try:
            load_template(options.template, self.templates_dir)
        except ConfigError as exc:
            raise HTTPException(400, detail=str(exc)) from None
        try:
            config.preflight(options.llm, options.publish)
        except ConfigError as exc:
            raise HTTPException(503, detail=f"server is not configured for this request: {exc}") from None
        return options

    def submit(self, kind: str, request: GenerateRequest, dedupe_key: str | None = None) -> tuple[Job, bool]:
        options = self.validate(request)
        job, created = self.store.create_job(kind, request.model_dump(mode="json"), dedupe_key)
        if created:
            self.pool.submit(self._run, job.id, request, options)
            log.info("job queued", extra={"job": job.id, "kind": kind, "tickets": len(request.tickets),
                                          "jql": request.jql or "", "target": options.publish})
        return job, created

    def _run(self, job_id: str, request: GenerateRequest, options: PipelineOptions) -> None:
        self.store.update_job(job_id, status="running")
        try:
            pipeline = Pipeline(options, store=self.store)
            tickets = list(request.tickets)
            if request.jql:
                matched = pipeline.jira.search(request.jql, request.max_results)
                tickets = list(dict.fromkeys([*tickets, *matched]))
            results = pipeline.run(tickets)
            ok = all(r.ok for r in results)
            self.store.update_job(job_id, status="succeeded" if ok else "failed",
                                  results=[r.model_dump(mode="json") for r in results],
                                  error=None if ok else "one or more tickets failed; see results")
            log.info("job finished", extra={"job": job_id, "ok": ok, "tickets": len(results)})
        except DocgenError as exc:
            self.store.update_job(job_id, status="failed", error=str(exc))
            log.error("job failed", extra={"job": job_id, "error": str(exc)})
        except Exception as exc:  # never leave a job "running" forever
            log.exception("job crashed", extra={"job": job_id})
            self.store.update_job(job_id, status="failed", error=f"internal error: {exc!r}")

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


# ── app ──────────────────────────────────────────────────────

def create_app(settings: DocgenSettings | None = None, *, store: Store | None = None,
               templates_dir: Path | None = None) -> FastAPI:
    settings = settings or config.load(DocgenSettings)
    store = store or Store(settings.store)
    runner = JobRunner(store, settings, templates_dir)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stale = store.mark_interrupted()
        if stale:
            log.warning("marked unfinished jobs from a previous run as interrupted", extra={"jobs": stale})
        if settings.api_token is None:
            log.warning("DOCGEN_API_TOKEN is not set — the API accepts unauthenticated requests")
        yield
        runner.shutdown()

    app = FastAPI(title="docgen", version=__version__, lifespan=lifespan,
                  description="Jira ticket → LLM → technical document, as a service.")
    app.state.runner, app.state.store, app.state.settings = runner, store, settings

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if settings.api_token is None:
            return
        expected = f"Bearer {settings.api_token.get_secret_value()}"
        if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(401, detail="missing or invalid bearer token",
                                headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(require_token)]

    def accepted(job: Job, created: bool) -> JSONResponse:
        body = JobAccepted(job_id=job.id, status=job.status, status_url=f"/status/{job.id}",
                           duplicate=not created)
        return JSONResponse(body.model_dump(), status_code=202 if created else 200)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/templates", dependencies=auth)
    def templates() -> list[dict]:
        out = []
        for name in available_templates(templates_dir):
            t = load_template(name, templates_dir)
            out.append({"name": name, "title": t.title, "description": t.description,
                        "sections": [s.title for s in t.sections]})
        return out

    @app.post("/generate", dependencies=auth, status_code=202, response_model=JobAccepted)
    def generate(request: GenerateRequest) -> JSONResponse:
        return accepted(*runner.submit("generate", request))

    @app.post("/publish", dependencies=auth, status_code=202, response_model=JobAccepted)
    def publish(request: PublishRequest) -> JSONResponse:
        return accepted(*runner.submit("publish", request))

    @app.get("/status/{job_id}", dependencies=auth, response_model=Job)
    def status(job_id: str) -> Job:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(404, detail=f"no job {job_id}")
        return job

    @app.get("/jobs", dependencies=auth, response_model=list[Job])
    def jobs(limit: int = Query(default=20, ge=1, le=200)) -> list[Job]:
        return store.list_jobs(limit)

    @app.get("/documents", dependencies=auth)
    def documents(ticket: str | None = None, include_markdown: bool = False) -> list[dict]:
        try:
            key = normalise_ticket_key(ticket) if ticket else None
        except InvalidTicketKeyError as exc:
            raise HTTPException(422, detail=exc.message) from None
        exclude = None if include_markdown else {"markdown"}
        return [d.model_dump(exclude=exclude) for d in store.list_documents(key)]

    @app.post("/webhooks/jira")
    async def jira_webhook(request: Request, token: str | None = Query(default=None)) -> JSONResponse:
        if settings.webhook_secret is None:
            raise HTTPException(503, detail="webhook disabled: set DOCGEN_WEBHOOK_SECRET")
        raw = await request.body()
        if not _webhook_authentic(raw, request.headers, token, settings.webhook_secret.get_secret_value()):
            raise HTTPException(401, detail="webhook signature or token missing or invalid")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise HTTPException(400, detail="body is not JSON") from None

        decision = webhook_decision(payload, settings.statuses)
        if decision["action"] == "ignored":
            log.info("webhook ignored", extra={"reason": decision["reason"],
                                               "ticket": decision.get("ticket", "")})
            return JSONResponse(decision, status_code=200)

        key, to_status = decision["ticket"], decision["status"]
        common: dict[str, Any] = {"tickets": [key],
                                  "template": settings.webhook_template or settings.template}
        job_request: GenerateRequest = (
            PublishRequest(**common, target=settings.webhook_publish, public=settings.webhook_public)
            if settings.webhook_publish != "none" else GenerateRequest(**common))
        identifier = request.headers.get("x-atlassian-webhook-identifier") or decision["change_id"]
        job, created = runner.submit("webhook", job_request,
                                     dedupe_key=f"jira:{key}:{to_status}:{identifier}")
        response = accepted(job, created)
        log.info("webhook queued" if created else "webhook duplicate",
                 extra={"ticket": key, "status": to_status, "job": job.id})
        return response

    return app


def _webhook_authentic(raw: bytes, headers, token: str | None, secret: str) -> bool:
    signature = headers.get("x-hub-signature")
    if signature:
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.strip().encode(), expected.encode())
    supplied: str | None = headers.get("x-docgen-webhook-token") or token
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode(), secret.encode())


def webhook_decision(payload: dict, statuses: set[str]) -> dict:
    """
    Decide what a Jira webhook means. Accepts Jira's `jira:issue_updated` events
    (status change in `changelog.items`) and workflow post-function webhooks
    (`transition.to_status`).
    """
    issue = payload.get("issue") or {}
    key = issue.get("key")
    if not key:
        return {"action": "ignored", "reason": "payload has no issue key"}
    try:
        key = normalise_ticket_key(key)
    except InvalidTicketKeyError:
        return {"action": "ignored", "reason": f"invalid issue key {key!r}"}

    changelog = payload.get("changelog") or {}
    change = next((item for item in changelog.get("items") or []
                   if str(item.get("fieldId") or item.get("field") or "").lower() == "status"), None)
    to_status = change.get("toString") if change else (payload.get("transition") or {}).get("to_status")
    if not to_status:
        return {"action": "ignored", "reason": "no status change in this event", "ticket": key}
    if to_status.casefold() not in statuses:
        return {"action": "ignored", "ticket": key,
                "reason": f"status {to_status!r} is not one of the configured webhook statuses"}
    change_id = str(changelog.get("id") or payload.get("timestamp") or "")
    return {"action": "queue", "ticket": key, "status": to_status, "change_id": change_id}


__all__ = ["GenerateRequest", "JobRunner", "PublishRequest", "StoredDocument", "create_app",
           "webhook_decision"]
