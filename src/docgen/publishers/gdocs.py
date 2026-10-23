"""
Google Docs publisher (OAuth 2.0 installed-app flow, Docs + Drive APIs).

The Docs API has no markdown importer: you insert plain text, then style it
by character range. markdown_to_docs() does that mapping, and because no
styling request changes text length, the whole document lands in a single
batchUpdate.

Updating an existing document happens in place, in that same single
batchUpdate: delete the old body, insert the new text, reset inherited
styles, restyle. The doc keeps its URL, sharing, comments and revision
history.

Each API step (create, write, share) retries on its own, so a throttled
share never re-creates the document. Writes are pinned to the revision read
just before, so if a timed-out write actually landed, the retry is rejected
by Google instead of applying the text a second time.

Images: the Docs API only embeds images from a URL it can fetch without our
credentials. Each image is uploaded to Drive, shared by link just long
enough to be inserted (Docs stores its own copy), then deleted. If Google
can't fetch it, the slot gets a text placeholder instead of failing the
publish.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from docgen.assets import image_name
from docgen.config import GoogleSettings, load
from docgen.errors import APIError, AuthError, ConfigError, DocgenError, RateLimitError, TransientError
from docgen.http import parse_retry_after
from docgen.models import Attachment, GeneratedDocument, Published
from docgen.publishers.markdown_convert import ImageSlot, markdown_to_docs_with_images
from docgen.retry import retry

log = logging.getLogger(__name__)

SERVICE = "google"
SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]
RATE_LIMIT_REASONS = ("rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED")
DISABLED_API_REASONS = ("accessNotConfigured", "SERVICE_DISABLED")
IMAGE_WIDTH_PT = 450   # fits the default page width; height follows the aspect ratio
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

    def __init__(self, settings: GoogleSettings | None = None, *, interactive: bool = True):
        self.settings = settings or load(GoogleSettings)
        # False in the server: never try to open a browser for consent.
        self.interactive = interactive
        self._creds: Credentials | None = None
        self._creds_lock = threading.Lock()
        self._local = threading.local()

    # ── auth ─────────────────────────────────────────────────

    def credentials(self) -> Credentials:
        with self._creds_lock:  # one consent / refresh, however many threads publish
            if self._creds is None or not self._creds.valid:
                self._creds = self._load_credentials()
            return self._creds

    def _load_credentials(self) -> Credentials:
        token_file = self.settings.token_file
        creds = None
        if token_file.exists():
            try:
                # No scopes argument: it would overwrite the token's stored scopes and make
                # the has_scopes() check below always pass.
                creds = Credentials.from_authorized_user_file(str(token_file))
            except (ValueError, KeyError) as exc:
                log.warning("ignoring unreadable token file", extra={"path": str(token_file),
                                                                     "error": str(exc)})

        if creds and creds.valid and creds.has_scopes(SCOPES):
            return creds

        if creds and creds.expired and creds.refresh_token and creds.has_scopes(SCOPES):
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

        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def _consent(self) -> Credentials:
        client_file = self.settings.credentials_file
        if not self.interactive:
            raise AuthError(
                f"No usable Google OAuth token at {self.settings.token_file}",
                hint="Run `docgen auth google` on a machine with a browser, then make the "
                     "resulting token.json available here (GOOGLE_TOKEN_FILE)")
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
        """Per-thread Docs and Drive clients: httplib2 connections are not thread-safe."""
        services = getattr(self._local, "services", None)
        if services is None:
            creds = self.credentials()

            def authed():
                return AuthorizedHttp(creds, http=httplib2.Http(timeout=self.settings.timeout))
            services = (build("docs", "v1", http=authed(), cache_discovery=False),
                        build("drive", "v3", http=authed(), cache_discovery=False))
            self._local.services = services
        return services

    # ── API steps, each retried independently ────────────────

    @retry()
    def _create(self, title: str) -> dict:
        docs, _ = self._services()
        return _execute(docs.documents().create(body={"title": title}), "documents.create")

    @retry()
    def _get(self, doc_id: str) -> dict | None:
        """Current revision and end index of an existing doc, or None if it is gone."""
        docs, drive = self._services()
        try:
            meta = _execute(drive.files().get(fileId=doc_id, fields="id,name,trashed"), "files.get")
            if meta.get("trashed"):
                return None
            return _execute(docs.documents().get(
                documentId=doc_id, fields="documentId,title,revisionId,body(content(endIndex))"),
                "documents.get")
        except APIError as exc:
            if exc.status == 404:
                return None
            raise

    @retry()
    def _write(self, doc_id: str, revision_id: str | None, requests_body: list) -> None:
        docs, _ = self._services()
        body: dict[str, Any] = {"requests": requests_body}
        if revision_id:
            body["writeControl"] = {"requiredRevisionId": revision_id}
        _execute(docs.documents().batchUpdate(documentId=doc_id, body=body),
                 "documents.batchUpdate")

    @retry()
    def _rename(self, doc_id: str, title: str) -> None:
        _, drive = self._services()
        _execute(drive.files().update(fileId=doc_id, body={"name": title}), "files.update")

    @retry()
    def _share(self, doc_id: str) -> None:
        _, drive = self._services()
        _execute(drive.permissions().create(fileId=doc_id,
                                            body={"type": "anyone", "role": "reader"}),
                 "permissions.create")

    @retry()
    def _upload_temporary(self, image: Attachment) -> dict:
        _, drive = self._services()
        media = MediaFileUpload(image.local_path, mimetype=image.mime_type, resumable=False)
        return _execute(drive.files().create(
            body={"name": f"docgen-temp-{image.filename}"}, media_body=media,
            fields="id,webContentLink"), "files.create")

    @retry()
    def _delete_file(self, file_id: str) -> None:
        _, drive = self._services()
        _execute(drive.files().delete(fileId=file_id), "files.delete")

    # ── publish ──────────────────────────────────────────────

    def publish(self, document: GeneratedDocument, *, public: bool = False,
                existing_id: str | None = None) -> Published:
        """
        Create a styled Doc, or update `existing_id` in place. Sharing is opt-in —
        internal docs stay private by default.
        """
        plain_text, style_requests, slots = markdown_to_docs_with_images(document.body)

        current = self._get(existing_id) if existing_id else None
        if existing_id and current is None:
            log.warning("linked Google Doc no longer exists; creating a new one",
                        extra={"doc_id": existing_id, "ticket": document.ticket_key})

        requests_body: list[dict] = []
        if current is None:
            created = True
            doc = self._create(document.title)
            doc_id, revision = doc["documentId"], doc.get("revisionId")
            requests_body = style_requests
            log.info("google doc created", extra={"doc_id": doc_id, "ticket": document.ticket_key})
        else:
            created = False
            doc_id, revision = current["documentId"], current.get("revisionId")
            if current.get("title") != document.title:
                self._rename(doc_id, document.title)
            requests_body = self._replacement_requests(current, plain_text, style_requests)

        if requests_body:
            self._write(doc_id, revision, requests_body)
            log.info("google doc written", extra={"doc_id": doc_id, "chars": len(plain_text),
                                                  "mode": "create" if created else "update"})
        elif created:
            log.warning("nothing to insert — document left empty", extra={"doc_id": doc_id})

        if slots:
            self._embed_images(doc_id, slots, document.images)

        if public:
            self._share(doc_id)
            log.info("google doc shared", extra={"doc_id": doc_id, "access": "anyone-with-link"})

        url = f"https://docs.google.com/document/d/{doc_id}"
        log.info("published", extra={"target": self.name, "ticket": document.ticket_key,
                                     "url": url, "action": "created" if created else "updated"})
        return Published(target=self.name, external_id=doc_id, url=url, created=created)

    @staticmethod
    def _replacement_requests(current: dict, plain_text: str, style_requests: list) -> list[dict]:
        """Delete the old body, insert the new text, clear inherited styles, then restyle."""
        content = (current.get("body") or {}).get("content") or []
        end_index = max((c.get("endIndex", 1) for c in content), default=1)
        requests: list[dict] = []
        # The final newline of a Doc cannot be deleted, so the range stops one short of it.
        if end_index - 1 > 1:
            requests.append({"deleteContentRange": {"range": {"startIndex": 1,
                                                              "endIndex": end_index - 1}}})
        if not style_requests:
            return requests
        insert, styles = style_requests[0], style_requests[1:]
        rng = {"startIndex": 1, "endIndex": 1 + len(plain_text)}
        requests += [
            insert,
            # Inserted text inherits the style of the paragraph it lands in, which may be
            # a leftover heading or bullet from the old body — reset before restyling.
            {"updateParagraphStyle": {"range": rng, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                      "fields": "namedStyleType"}},
            {"deleteParagraphBullets": {"range": rng}},
            {"updateTextStyle": {"range": rng, "textStyle": {},
                                 "fields": "bold,italic,underline,weightedFontFamily"}},
            *styles,
        ]
        return requests

    def _embed_images(self, doc_id: str, slots: list[ImageSlot], images: list[Attachment]) -> None:
        by_name = {image_name(img): img for img in images if img.local_path}
        # Descending index order: each insertion only shifts the slots after it.
        for slot in sorted(slots, key=lambda s: s.position, reverse=True):
            image = by_name.get(slot.filename)
            request: dict
            temp_id = None
            try:
                if image is None:
                    raise APIError(f"image {slot.filename} was not downloaded", service=SERVICE)
                uploaded = self._upload_temporary(image)
                temp_id = uploaded["id"]
                self._share(temp_id)
                request = {"insertInlineImage": {
                    "location": {"index": slot.position},
                    "uri": uploaded.get("webContentLink")
                           or f"https://drive.google.com/uc?export=download&id={temp_id}",
                    "objectSize": {"width": {"magnitude": IMAGE_WIDTH_PT, "unit": "PT"}}}}
                self._write(doc_id, None, [request])
                log.info("image embedded", extra={"doc_id": doc_id, "image": slot.filename})
            except (APIError, RateLimitError, TransientError) as exc:
                log.warning("image could not be embedded; leaving a placeholder",
                            extra={"doc_id": doc_id, "image": slot.filename, "error": exc.message})
                placeholder = f"[image: {slot.caption or slot.filename}]"
                self._write(doc_id, None, [{"insertText": {"location": {"index": slot.position},
                                                           "text": placeholder}}])
            finally:
                if temp_id:
                    try:
                        self._delete_file(temp_id)
                    except DocgenError as exc:
                        log.warning("temporary Drive image not deleted",
                                    extra={"file_id": temp_id, "error": exc.message})
