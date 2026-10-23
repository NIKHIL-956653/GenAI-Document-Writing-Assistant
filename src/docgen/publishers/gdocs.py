"""
Google Docs publisher (OAuth 2.0 installed-app flow, Docs + Drive APIs).

The Docs API has no markdown importer: you insert plain text, then style it
by character range. markdown_to_docs() does that mapping, and because no
styling request changes text length, the whole document lands in a single
batchUpdate.

Each API step (create, write, share) retries on its own, so a throttled
share never re-creates the document. The write is pinned to the revision
returned by create, so if a timed-out write actually landed, the retry is
rejected by Google instead of inserting the text a second time.
"""
from __future__ import annotations

import logging

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from docgen.config import GoogleSettings, load
from docgen.errors import (APIError, AuthError, ConfigError, DocgenError, RateLimitError,
                           TransientError)
from docgen.http import parse_retry_after
from docgen.models import GeneratedDocument
from docgen.publishers.markdown_convert import markdown_to_docs
from docgen.retry import retry

log = logging.getLogger(__name__)

SERVICE = "google"
SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]
RATE_LIMIT_REASONS = ("rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED")
DISABLED_API_REASONS = ("accessNotConfigured", "SERVICE_DISABLED")
REAUTH_HINT = ("Delete {token} and re-run to sign in again. If your OAuth consent screen is in "
               "'Testing' status, Google expires refresh tokens after 7 days — publish the app "
               "to avoid this")


def classify_google_error(exc: Exception, what: str) -> DocgenError:
    """Map a google-api-python-client / transport exception onto our error types."""
    if isinstance(exc, HttpError):
        status = exc.resp.status
        reason = f"{getattr(exc, 'reason', '')} {exc.error_details}"
        detail = f"Google API {what} failed with HTTP {status}: {getattr(exc, 'reason', '')}"
        if status == 429 or (status == 403 and any(r in reason for r in RATE_LIMIT_REASONS)):
            return RateLimitError(detail, service=SERVICE, status=status,
                                  retry_after=parse_retry_after(exc.resp.get("retry-after")))
        if status >= 500 or status == 408:
            return TransientError(detail, service=SERVICE, status=status)
        if status == 401:
            return AuthError(detail, hint="The Google OAuth token was rejected — delete the "
                                          "token file and re-run to sign in again")
        if status == 403 and any(r in reason for r in DISABLED_API_REASONS):
            return ConfigError(detail, hint="Enable the Google Docs API and Google Drive API for "
                                            "your OAuth client's project in Google Cloud Console")
        return APIError(detail, service=SERVICE, status=status)
    if isinstance(exc, RefreshError):
        return AuthError(f"Google OAuth token could not be refreshed during {what}: {exc}",
                         hint=REAUTH_HINT.format(token="the token file (GOOGLE_TOKEN_FILE)"))
    # Timeouts, dropped connections, DNS failures, transport errors
    return TransientError(f"Google API {what}: {type(exc).__name__}: {exc}", service=SERVICE)


def _execute(request, what: str):
    try:
        return request.execute(num_retries=0)
    except (HttpError, RefreshError, TransportError, httplib2.HttpLib2Error, OSError) as exc:
        raise classify_google_error(exc, what) from exc


class GoogleDocsPublisher:
    name = "gdocs"

    def __init__(self, settings: GoogleSettings | None = None):
        self.settings = settings or load(GoogleSettings)
        self._docs = None
        self._drive = None

    # ── auth ─────────────────────────────────────────────────

    def credentials(self) -> Credentials:
        token_file = self.settings.token_file
        creds = None
        if token_file.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
            except (ValueError, KeyError) as exc:
                log.warning("ignoring unreadable token file", extra={"path": str(token_file),
                                                                     "error": str(exc)})

        if creds and creds.valid and creds.has_scopes(SCOPES):
            return creds

        if creds and creds.expired and creds.refresh_token:
            log.info("refreshing Google OAuth token")
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise AuthError(f"Google OAuth token in {token_file} has expired or been revoked "
                                f"({exc})", hint=REAUTH_HINT.format(token=token_file)) from exc
            except TransportError as exc:
                raise TransientError(f"could not reach Google to refresh the OAuth token: {exc}",
                                     service=SERVICE) from exc
        else:
            creds = self._consent()

        token_file.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def _consent(self) -> Credentials:
        client_file = self.settings.credentials_file
        if not client_file.exists():
            raise ConfigError(
                f"Google OAuth client file not found at {client_file.resolve()}",
                hint="Download an OAuth 2.0 'Desktop app' client from Google Cloud Console "
                     "(APIs & Services → Credentials), save it there, or set GOOGLE_CREDENTIALS_FILE",
            )
        log.info("opening browser for Google sign-in", extra={"client": str(client_file)})
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
        except ValueError as exc:
            raise ConfigError(f"{client_file} is not a valid OAuth client file: {exc}",
                              hint="It must be the JSON for an OAuth client of type "
                                   "'Desktop app', not a service account key") from exc
        return flow.run_local_server(port=0, timeout_seconds=300)

    def _services(self):
        if self._docs is None:
            creds = self.credentials()
            # Each service gets its own Http: httplib2 connections are not thread-safe,
            # and this is where the per-request timeout is set.
            def authed():
                return AuthorizedHttp(creds, http=httplib2.Http(timeout=self.settings.timeout))
            self._docs = build("docs", "v1", http=authed(), cache_discovery=False)
            self._drive = build("drive", "v3", http=authed(), cache_discovery=False)
        return self._docs, self._drive

    # ── API steps, each retried independently ────────────────

    @retry()
    def _create(self, title: str) -> dict:
        docs, _ = self._services()
        return _execute(docs.documents().create(body={"title": title}), "documents.create")

    @retry()
    def _write(self, doc_id: str, revision_id: str | None, requests_body: list) -> None:
        docs, _ = self._services()
        body = {"requests": requests_body}
        if revision_id:
            body["writeControl"] = {"requiredRevisionId": revision_id}
        _execute(docs.documents().batchUpdate(documentId=doc_id, body=body),
                 "documents.batchUpdate")

    @retry()
    def _share(self, doc_id: str) -> None:
        _, drive = self._services()
        _execute(drive.permissions().create(fileId=doc_id,
                                            body={"type": "anyone", "role": "reader"}),
                 "permissions.create")

    def publish(self, document: GeneratedDocument, *, public: bool = False) -> str:
        """Create a styled Doc. Sharing is opt-in — internal docs stay private by default."""
        doc = self._create(document.title)
        doc_id = doc["documentId"]
        log.info("google doc created", extra={"doc_id": doc_id, "ticket": document.ticket_key})

        plain_text, requests_body = markdown_to_docs(document.body)
        if not requests_body:
            log.warning("nothing to insert — document left empty", extra={"doc_id": doc_id})
        else:
            self._write(doc_id, doc.get("revisionId"), requests_body)
            log.info("google doc written", extra={"doc_id": doc_id, "chars": len(plain_text),
                                                  "style_requests": len(requests_body) - 1})

        if public:
            self._share(doc_id)
            log.info("google doc shared", extra={"doc_id": doc_id, "access": "anyone-with-link"})

        url = f"https://docs.google.com/document/d/{doc_id}"
        log.info("published", extra={"target": self.name, "ticket": document.ticket_key,
                                     "url": url})
        return url
