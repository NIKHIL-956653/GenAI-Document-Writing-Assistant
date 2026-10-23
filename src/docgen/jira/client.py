"""Jira REST v3 client."""
from __future__ import annotations

import logging

import requests
from requests.auth import HTTPBasicAuth

from docgen import http
from docgen.config import JiraSettings, load
from docgen.errors import APIError, AuthError, DocgenError, TicketNotFoundError
from docgen.jira.adf import adf_to_text
from docgen.models import Ticket, normalise_ticket_key
from docgen.retry import retry

log = logging.getLogger(__name__)

FIELDS = "summary,description,status,labels"


class JiraClient:
    def __init__(self, settings: JiraSettings | None = None):
        self.settings = settings or load(JiraSettings)
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(self.settings.email,
                                          self.settings.token.get_secret_value())
        self.session.headers["Accept"] = "application/json"

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
    def get_ticket(self, key: str) -> Ticket:
        key = normalise_ticket_key(key)
        response = http.send(
            "GET", f"{self.settings.base_url}/rest/api/3/issue/{key}",
            service="jira", timeout=self.settings.timeout, session=self.session,
            params={"fields": FIELDS},
            on_status=lambda r: self._status_error(key, r),
            unreachable_hint=f"Check JIRA_DOMAIN ({self.settings.domain}) and your network",
        )
        try:
            data = response.json()
            fields = data["fields"]
            ticket = Ticket(
                key=data["key"],
                title=fields["summary"],
                description=adf_to_text(fields.get("description")).strip() or "No description",
                status=(fields.get("status") or {}).get("name", "Unknown"),
                labels=fields.get("labels"),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise APIError(f"unexpected Jira response for {key}: {exc!r}", service="jira") from exc

        log.info("ticket fetched", extra={"ticket": ticket.key, "title": ticket.title,
                                          "status": ticket.status})
        return ticket
