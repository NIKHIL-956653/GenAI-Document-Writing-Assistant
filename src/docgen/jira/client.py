"""
Jira REST v3 client: one issue with as much surrounding context as asked
for, JQL search for batch runs, and attachment downloads.

Safe to share across threads: each thread gets its own requests.Session.
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

from docgen import http
from docgen.config import JiraSettings, load
from docgen.errors import (
    APIError,
    AuthError,
    DocgenError,
    InvalidJQLError,
    TicketNotFoundError,
    TransientError,
)
from docgen.jira.adf import adf_to_text
from docgen.models import (
    Attachment,
    Comment,
    ContextLevel,
    ContextOptions,
    RelatedIssue,
    Ticket,
    normalise_ticket_key,
)
from docgen.retry import retry

log = logging.getLogger(__name__)

BASE_FIELDS = ["summary", "description", "status", "labels", "issuetype", "priority"]
SEARCH_PAGE_SIZE = 100
DEFAULT_CACHE_DIR = Path(".docgen") / "attachments"


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "attachment"


def _name(field: dict | None, default: str = "") -> str:
    return (field or {}).get("name", default) if isinstance(field, dict) else default


class JiraClient:
    def __init__(self, settings: JiraSettings | None = None, cache_dir: Path | None = None):
        self.settings = settings or load(JiraSettings)
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        """One Session per thread: Sessions are not guaranteed thread-safe."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.auth = HTTPBasicAuth(self.settings.email, self.settings.token.get_secret_value())
            session.headers["Accept"] = "application/json"
            self._local.session = session
        return session

    def _status_error(self, key: str, response: requests.Response) -> DocgenError | None:
        if response.status_code == 401:
            return AuthError(
                f"Jira rejected the credentials for {self.settings.email} (HTTP 401)",
                hint="JIRA_TOKEN may be wrong, revoked or expired — create a new one at "
                     "https://id.atlassian.com/manage-profile/security/api-tokens, and check "
                     "JIRA_EMAIL is the address that owns it",
            )
        if response.status_code == 403:
            return AuthError(
                f"Jira denied access to {key} (HTTP 403)",
                hint=f"{self.settings.email} needs 'Browse projects' permission on that project",
            )
        if response.status_code == 404:
            return TicketNotFoundError(
                f"Ticket {key} was not found on {self.settings.domain}",
                hint="Check the key is spelled correctly and that your account can see the "
                     "project — Jira returns 404 for issues you lack permission to view",
            )
        return None

    @retry()
    def _fetch_issue(self, key: str, fields: list[str]) -> dict:
        response = http.send(
            "GET", f"{self.settings.base_url}/rest/api/3/issue/{key}",
            service="jira", timeout=self.settings.timeout, session=self.session,
            params={"fields": ",".join(fields)},
            on_status=lambda r: self._status_error(key, r),
            unreachable_hint=f"Check JIRA_DOMAIN ({self.settings.domain}) and your network",
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise APIError(f"unexpected Jira response for {key}: {exc!r}", service="jira") from exc
        if not isinstance(data, dict):
            raise APIError(f"unexpected Jira response for {key}: not an object", service="jira")
        return data

    # ── single issue ─────────────────────────────────────────

    def get_ticket(self, key: str,
                   context: ContextLevel | ContextOptions = ContextLevel.MINIMAL) -> Ticket:
        """
        Fetch an issue. `context` controls how much is pulled alongside the
        description — see ContextLevel. Extra context is best-effort: a linked
        issue you can't see, or an attachment that fails to download, is
        logged and skipped rather than failing the ticket.
        """
        key = normalise_ticket_key(key)
        options = context if isinstance(context, ContextOptions) else ContextOptions.for_level(context)

        fields = list(BASE_FIELDS)
        if options.max_comments:
            fields.append("comment")
        if options.links:
            fields.append("issuelinks")
        if options.subtasks:
            fields.append("subtasks")
        if options.attachments:
            fields.append("attachment")

        data = self._fetch_issue(key, fields)
        try:
            ticket = self._parse(data, options)
        except (KeyError, TypeError, ValueError) as exc:
            raise APIError(f"unexpected Jira response for {key}: {exc!r}", service="jira") from exc

        if options.related_descriptions:
            self._add_related_descriptions(ticket, options)
        if options.download_images:
            self._download_images(ticket, options)

        log.info("ticket fetched", extra={
            "ticket": ticket.key, "title": ticket.title, "status": ticket.status,
            "context": "custom" if isinstance(context, ContextOptions) else ContextLevel(context).value,
            "comments": len(ticket.comments), "links": len(ticket.links),
            "subtasks": len(ticket.subtasks), "images": len(ticket.images)})
        return ticket

    def _parse(self, data: dict, options: ContextOptions) -> Ticket:
        fields = data["fields"]
        comments: list[Comment] = []
        if options.max_comments:
            raw = (fields.get("comment") or {}).get("comments") or []
            for c in raw[-options.max_comments:]:
                body = adf_to_text(c.get("body")).strip()
                if body:
                    comments.append(Comment(author=(c.get("author") or {}).get("displayName", "unknown"),
                                            created=c.get("created", ""), body=body))

        links: list[RelatedIssue] = []
        for link in (fields.get("issuelinks") or []) if options.links else []:
            kind = link.get("type") or {}
            if "outwardIssue" in link:
                other, relation = link["outwardIssue"], kind.get("outward", "relates to")
            elif "inwardIssue" in link:
                other, relation = link["inwardIssue"], kind.get("inward", "relates to")
            else:
                continue
            other_fields = other.get("fields") or {}
            links.append(RelatedIssue(key=other["key"], relation=relation,
                                      summary=other_fields.get("summary", ""),
                                      status=_name(other_fields.get("status"))))

        subtasks = [RelatedIssue(key=s["key"], summary=(s.get("fields") or {}).get("summary", ""),
                                 status=_name((s.get("fields") or {}).get("status")))
                    for s in ((fields.get("subtasks") or []) if options.subtasks else [])]

        attachments = [Attachment(id=str(a["id"]), filename=a.get("filename", "attachment"),
                                  mime_type=a.get("mimeType", "application/octet-stream"),
                                  size=int(a.get("size") or 0), content_url=a.get("content", ""))
                       for a in ((fields.get("attachment") or []) if options.attachments else [])]

        return Ticket(
            key=data["key"],
            title=fields["summary"],
            description=adf_to_text(fields.get("description")).strip() or "No description",
            status=_name(fields.get("status"), "Unknown") or "Unknown",
            labels=fields.get("labels"),
            issue_type=_name(fields.get("issuetype")) or None,
            priority=_name(fields.get("priority")) or None,
            comments=comments, links=links, subtasks=subtasks, attachments=attachments,
        )

    def _add_related_descriptions(self, ticket: Ticket, options: ContextOptions) -> None:
        for item in [*ticket.subtasks, *ticket.links][:options.max_related]:
            try:
                data = self._fetch_issue(item.key, ["description"])
                item.description = adf_to_text(data["fields"].get("description")).strip() or None
            except (DocgenError, KeyError, TypeError) as exc:
                log.warning("skipping related issue description",
                            extra={"ticket": ticket.key, "related": item.key,
                                   "error": getattr(exc, "message", repr(exc))})

    # ── attachments ──────────────────────────────────────────

    def _download_images(self, ticket: Ticket, options: ContextOptions) -> None:
        folder = self.cache_dir / ticket.key
        images = [a for a in ticket.attachments if a.is_image]
        for attachment in images[:options.max_images]:
            if attachment.size > options.max_image_bytes:
                log.warning("skipping large image", extra={"ticket": ticket.key,
                                                           "file": attachment.filename,
                                                           "bytes": attachment.size})
                continue
            try:
                path = folder / f"{attachment.id}-{_safe_filename(attachment.filename)}"
                if not (path.exists() and path.stat().st_size == attachment.size):
                    folder.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(self._download(attachment, options.max_image_bytes))
                attachment.local_path = str(path)
            except DocgenError as exc:
                log.warning("attachment download failed", extra={
                    "ticket": ticket.key, "file": attachment.filename, "error": exc.message})
        if len(images) > options.max_images:
            log.warning("too many images; extra ones skipped",
                        extra={"ticket": ticket.key, "images": len(images),
                               "max_images": options.max_images})

    @retry()
    def _download(self, attachment: Attachment, max_bytes: int) -> bytes:
        response = http.send(
            "GET", attachment.content_url, service="jira", timeout=self.settings.timeout,
            session=self.session, stream=True,
            on_status=lambda r: self._status_error(attachment.filename, r),
        )
        chunks, total = [], 0
        try:
            for chunk in response.iter_content(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise APIError(f"attachment {attachment.filename} is larger than {max_bytes} bytes",
                                   service="jira")
                chunks.append(chunk)
        except requests.RequestException as exc:
            raise TransientError(f"attachment download interrupted: {exc}", service="jira") from exc
        finally:
            response.close()
        return b"".join(chunks)

    # ── search ───────────────────────────────────────────────

    def _search_error(self, response: requests.Response) -> DocgenError | None:
        if response.status_code == 400:
            try:
                messages = response.json().get("errorMessages") or []
            except ValueError:
                messages = []
            detail = "; ".join(messages) or response.text[:300]
            return InvalidJQLError(f"Jira rejected the JQL query: {detail}",
                                   hint="Test the query in Jira's issue search (Filters → Advanced) first")
        return self._status_error("the search", response)

    @retry()
    def _search_page(self, jql: str, page_size: int, token: str | None) -> dict:
        body: dict = {"jql": jql, "fields": ["summary"], "maxResults": page_size}
        if token:
            body["nextPageToken"] = token
        response = http.send(
            "POST", f"{self.settings.base_url}/rest/api/3/search/jql",
            service="jira", timeout=self.settings.timeout, session=self.session,
            json=body, on_status=self._search_error,
            unreachable_hint=f"Check JIRA_DOMAIN ({self.settings.domain}) and your network",
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise APIError(f"unexpected Jira search response: {exc!r}", service="jira") from exc
        if not isinstance(data, dict) or not isinstance(data.get("issues"), list):
            raise APIError("unexpected Jira search response: no issues list", service="jira")
        return data

    def search(self, jql: str, max_results: int = 100) -> list[str]:
        """
        Issue keys matching `jql`, up to `max_results`, in Jira's order.
        Uses the token-paginated /rest/api/3/search/jql endpoint (the older
        /rest/api/3/search has been retired by Atlassian).
        """
        keys: list[str] = []
        token = None
        while len(keys) < max_results:
            page = self._search_page(jql, min(SEARCH_PAGE_SIZE, max_results - len(keys)), token)
            keys += [issue["key"] for issue in page["issues"] if "key" in issue]
            token = page.get("nextPageToken")
            if page.get("isLast", True) or not token or not page["issues"]:
                break
        keys = list(dict.fromkeys(keys))[:max_results]
        log.info("jql search", extra={"jql": jql, "matches": len(keys), "max_results": max_results})
        return keys
