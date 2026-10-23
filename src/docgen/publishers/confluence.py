"""Confluence Cloud publisher (reuses the Jira credentials)."""
from __future__ import annotations

import logging

import requests
from requests.auth import HTTPBasicAuth

from docgen import http
from docgen.assets import image_name
from docgen.config import ConfluenceSettings, JiraSettings, load
from docgen.errors import APIError, AuthError, DocgenError
from docgen.models import Attachment, GeneratedDocument, Published
from docgen.publishers.markdown_convert import markdown_to_storage
from docgen.retry import retry

log = logging.getLogger(__name__)

SERVICE = "confluence"


class ConfluencePublisher:
    name = "confluence"

    def __init__(self, jira: JiraSettings | None = None,
                 settings: ConfluenceSettings | None = None):
        self.jira = jira or load(JiraSettings)
        self.settings = settings or load(ConfluenceSettings)

    def _status_error(self, response: requests.Response) -> DocgenError | None:
        status, space = response.status_code, self.settings.space_key
        if status == 401:
            return AuthError("Confluence rejected the Atlassian credentials (HTTP 401)",
                             hint="Confluence uses JIRA_EMAIL / JIRA_TOKEN — check they are valid")
        if status == 403:
            return AuthError(f"No permission to create pages in Confluence space {space} (HTTP 403)",
                             hint="Ask a space admin for 'Add page' permission, or set "
                                  "CONFLUENCE_SPACE_KEY to a space you can write to")
        if status == 404:
            return APIError(f"Confluence space {space} not found on {self.jira.domain} (HTTP 404)",
                            service=SERVICE, status=status,
                            hint="Set CONFLUENCE_SPACE_KEY to an existing space key")
        if status == 400 and "already exists" in response.text:
            return APIError("A Confluence page with this title already exists in space "
                            f"{space}", service=SERVICE, status=status,
                            hint="Rename or delete the existing page, then re-run")
        return None

    def _auth(self) -> HTTPBasicAuth:
        return HTTPBasicAuth(self.jira.email, self.jira.token.get_secret_value())

    def _body(self, document: GeneratedDocument) -> dict:
        return {"storage": {"value": markdown_to_storage(document.body), "representation": "storage"}}

    def _page_url(self, response: requests.Response) -> tuple[str, str]:
        try:
            data = response.json()
            return str(data["id"]), f"{self.jira.base_url}/wiki{data['_links']['webui']}"
        except (ValueError, KeyError, TypeError) as exc:
            raise APIError("Confluence response had no page id or link", service=SERVICE) from exc

    @retry()
    def _create(self, document: GeneratedDocument) -> tuple[str, str]:
        response = http.send(
            "POST", f"{self.jira.base_url}/wiki/rest/api/content",
            service=SERVICE, timeout=self.settings.timeout, auth=self._auth(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={"type": "page", "title": document.title, "space": {"key": self.settings.space_key},
                  "body": self._body(document)},
            on_status=self._status_error,
        )
        return self._page_url(response)

    @retry()
    def _current_version(self, page_id: str) -> int | None:
        """The page's version number, or None if it was deleted."""
        response = http.send(
            "GET", f"{self.jira.base_url}/wiki/rest/api/content/{page_id}",
            service=SERVICE, timeout=self.settings.timeout, auth=self._auth(),
            headers={"Accept": "application/json"}, params={"expand": "version", "status": "current"},
            on_status=lambda r: (APIError("page gone", service=SERVICE, status=404)
                                 if r.status_code == 404 else self._status_error(r)),
        )
        try:
            return int(response.json()["version"]["number"])
        except (ValueError, KeyError, TypeError) as exc:
            raise APIError("Confluence page response had no version", service=SERVICE) from exc

    @retry()
    def _update(self, page_id: str, version: int, document: GeneratedDocument) -> tuple[str, str]:
        # Confluence rejects an update whose version isn't exactly current + 1 (HTTP 409), which
        # also makes a retried update after a lost response fail safely instead of applying twice.
        response = http.send(
            "PUT", f"{self.jira.base_url}/wiki/rest/api/content/{page_id}",
            service=SERVICE, timeout=self.settings.timeout, auth=self._auth(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={"id": page_id, "type": "page", "title": document.title,
                  "version": {"number": version + 1, "message": "Updated by docgen"},
                  "body": self._body(document)},
            on_status=self._status_error,
        )
        return self._page_url(response)

    @retry()
    def _upload(self, page_id: str, image: Attachment) -> None:
        # PUT creates the attachment or adds a new version of an existing one with that name.
        with open(image.local_path or "", "rb") as fh:
            http.send(
                "PUT", f"{self.jira.base_url}/wiki/rest/api/content/{page_id}/child/attachment",
                service=SERVICE, timeout=self.settings.timeout, auth=self._auth(),
                headers={"X-Atlassian-Token": "no-check", "Accept": "application/json"},
                files={"file": (image_name(image), fh, image.mime_type)},
                data={"minorEdit": "true"},
                on_status=self._status_error,
            )

    def publish(self, document: GeneratedDocument, *, public: bool = False,
                existing_id: str | None = None) -> Published:
        """
        Create a page from Markdown, or update `existing_id` in place (new page version).
        `public` is ignored: space permissions apply.
        """
        version = None
        if existing_id:
            try:
                version = self._current_version(existing_id)
            except APIError as exc:
                if exc.status != 404:
                    raise
                log.warning("linked Confluence page no longer exists; creating a new one",
                            extra={"page_id": existing_id, "ticket": document.ticket_key})

        if existing_id and version is not None:
            page_id, url = self._update(existing_id, version, document)
            created = False
        else:
            page_id, url = self._create(document)
            created = True

        for image in document.images:
            if image.local_path:
                try:
                    self._upload(page_id, image)
                except DocgenError as exc:
                    log.warning("image upload failed", extra={"page_id": page_id,
                                                              "image": image.filename,
                                                              "error": exc.message})

        log.info("published", extra={"target": self.name, "ticket": document.ticket_key,
                                     "space": self.settings.space_key, "url": url,
                                     "action": "created" if created else "updated"})
        return Published(target=self.name, external_id=page_id, url=url, created=created)
