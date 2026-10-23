"""
Local SQLite store.

`documents` maps (ticket, template, target) to the document that was
published for it — the Google Doc id, Confluence page id, or local file —
together with the Markdown last published and a fingerprint of everything
the model saw. That is what makes re-runs incremental: an unchanged ticket
is skipped, a changed one updates its existing document in place and gets a
diff against the stored Markdown.

`jobs` records work submitted to the HTTP server, so `GET /status/{id}`
survives a restart (jobs that were running when the server stopped are
marked `interrupted`).

Every call opens its own connection, so one Store is safe to share between
threads; WAL mode lets readers proceed while a writer commits.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1
DEFAULT_STORE = Path(".docgen") / "docgen.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    ticket_key   TEXT NOT NULL,
    template     TEXT NOT NULL,
    target       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    url          TEXT NOT NULL,
    title        TEXT NOT NULL,
    source_hash  TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    markdown     TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (ticket_key, template, target)
);
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    request     TEXT NOT NULL,
    results     TEXT,
    error       TEXT,
    dedupe_key  TEXT UNIQUE,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs (created_at);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StoredDocument(BaseModel):
    ticket_key: str
    template: str
    target: str               # gdocs | confluence | local
    external_id: str          # Google Doc id, Confluence page id, or file path
    url: str
    title: str
    source_hash: str
    content_hash: str
    markdown: str
    version: int = 1
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)


class Job(BaseModel):
    id: str
    kind: str                 # generate | publish | webhook
    status: str               # queued | running | succeeded | failed | interrupted
    request: dict[str, Any]
    results: list[dict[str, Any]] | None = None
    error: str | None = None
    dedupe_key: str | None = None
    created_at: str
    updated_at: str


class Store:
    def __init__(self, path: Path | str = DEFAULT_STORE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:  # commits on success, rolls back on error
                yield db
        finally:
            db.close()

    # ── documents ────────────────────────────────────────────

    def get_document(self, ticket_key: str, template: str, target: str) -> StoredDocument | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM documents WHERE ticket_key=? AND template=? AND target=?",
                             (ticket_key, template, target)).fetchone()
        return StoredDocument(**dict(row)) if row else None

    def save_document(self, doc: StoredDocument) -> StoredDocument:
        """Insert, or replace the row for the same (ticket, template, target)."""
        doc = doc.model_copy(update={"updated_at": now()})
        with self._connect() as db:
            db.execute(
                """INSERT INTO documents (ticket_key, template, target, external_id, url, title,
                       source_hash, content_hash, markdown, version, created_at, updated_at)
                   VALUES (:ticket_key, :template, :target, :external_id, :url, :title,
                       :source_hash, :content_hash, :markdown, :version, :created_at, :updated_at)
                   ON CONFLICT (ticket_key, template, target) DO UPDATE SET
                       external_id=excluded.external_id, url=excluded.url, title=excluded.title,
                       source_hash=excluded.source_hash, content_hash=excluded.content_hash,
                       markdown=excluded.markdown, version=excluded.version,
                       updated_at=excluded.updated_at""",
                doc.model_dump())
        return doc

    def list_documents(self, ticket_key: str | None = None, limit: int = 500) -> list[StoredDocument]:
        query, args = "SELECT * FROM documents", list[Any]()
        if ticket_key:
            query += " WHERE ticket_key=?"
            args.append(ticket_key)
        query += " ORDER BY updated_at DESC, ticket_key LIMIT ?"
        args.append(limit)
        with self._connect() as db:
            return [StoredDocument(**dict(r)) for r in db.execute(query, args).fetchall()]

    # ── jobs ─────────────────────────────────────────────────

    def create_job(self, kind: str, request: dict, dedupe_key: str | None = None) -> tuple[Job, bool]:
        """Returns (job, created). With a dedupe_key already seen, returns the existing job."""
        stamp = now()
        job = Job(id=uuid.uuid4().hex, kind=kind, status="queued", request=request,
                  dedupe_key=dedupe_key, created_at=stamp, updated_at=stamp)
        try:
            with self._connect() as db:
                db.execute("INSERT INTO jobs (id, kind, status, request, dedupe_key, created_at, updated_at)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (job.id, kind, job.status, json.dumps(request), dedupe_key, stamp, stamp))
        except sqlite3.IntegrityError:
            existing = self.find_job(dedupe_key) if dedupe_key else None
            if existing is None:
                raise
            return existing, False
        return job, True

    def update_job(self, job_id: str, *, status: str, results: list[dict] | None = None,
                   error: str | None = None) -> None:
        with self._connect() as db:
            db.execute("UPDATE jobs SET status=?, results=COALESCE(?, results), error=?, updated_at=?"
                       " WHERE id=?",
                       (status, json.dumps(results) if results is not None else None, error, now(), job_id))

    def _job(self, row: sqlite3.Row | None) -> Job | None:
        if row is None:
            return None
        data = dict(row)
        data["request"] = json.loads(data["request"])
        data["results"] = json.loads(data["results"]) if data["results"] else None
        return Job(**data)

    def get_job(self, job_id: str) -> Job | None:
        with self._connect() as db:
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def find_job(self, dedupe_key: str) -> Job | None:
        with self._connect() as db:
            return self._job(db.execute("SELECT * FROM jobs WHERE dedupe_key=?", (dedupe_key,)).fetchone())

    def list_jobs(self, limit: int = 50) -> list[Job]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                              (limit,)).fetchall()
        return [j for j in (self._job(r) for r in rows) if j is not None]

    def mark_interrupted(self) -> int:
        """At server start: jobs left queued/running by a previous process will never finish."""
        with self._connect() as db:
            cursor = db.execute("UPDATE jobs SET status='interrupted', updated_at=?,"
                                " error=COALESCE(error, 'server restarted before the job finished')"
                                " WHERE status IN ('queued', 'running')", (now(),))
            return cursor.rowcount
