"""Confluence Cloud publisher (reuses the Jira credentials)."""
from __future__ import annotations

import logging

import requests
from requests.auth import HTTPBasicAuth

from docgen import http
from docgen.config import ConfluenceSettings, JiraSettings, load
from docgen.errors import APIError, AuthError, DocgenError
from docgen.models import GeneratedDocument
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

    @retry()
    def publish(self, document: GeneratedDocument, *, public: bool = False) -> str:
        """Create a page from Markdown. `public` is ignored: space permissions apply."""
        response = http.send(
            "POST", f"{self.jira.base_url}/wiki/rest/api/content",
            service=SERVICE, timeout=self.settings.timeout,
            auth=HTTPBasicAuth(self.jira.email, self.jira.token.get_secret_value()),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={
                "type": "page",
                "title": document.title,
                "space": {"key": self.settings.space_key},
                "body": {"storage": {"value": markdown_to_storage(document.body),
                                     "representation": "storage"}},
            },
            on_status=self._status_error,
        )
        try:
            url = f"{self.jira.base_url}/wiki{response.json()['_links']['webui']}"
        except (ValueError, KeyError, TypeError) as exc:
            raise APIError("Confluence response had no page link", service=SERVICE) from exc
        log.info("published", extra={"target": self.name, "ticket": document.ticket_key,
                                     "space": self.settings.space_key, "url": url})
        return url
