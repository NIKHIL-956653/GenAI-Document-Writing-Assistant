import json

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from docgen.errors import APIError, AuthError, ConfigError, RateLimitError, TransientError
from docgen.publishers.gdocs import SCOPES, GoogleDocsPublisher, classify_google_error


def http_error(status, reason="", headers=None):
    resp = httplib2.Response({"status": status, **(headers or {})})
    resp.reason = reason
    content = json.dumps({"error": {"code": status, "message": reason,
                                    "errors": [{"reason": reason}]}}).encode()
    return HttpError(resp, content)


@pytest.mark.parametrize("status, reason, expected", [
    (429, "", RateLimitError),
    (403, "userRateLimitExceeded", RateLimitError),
    (403, "rateLimitExceeded", RateLimitError),
    (500, "", TransientError),
    (503, "", TransientError),
    (401, "", AuthError),
    (403, "accessNotConfigured", ConfigError),
    (400, "badRequest", APIError),
])
def test_google_http_errors_are_classified(status, reason, expected):
    err = classify_google_error(http_error(status, reason), "documents.create")
    assert type(err) is expected


def test_google_rate_limit_reads_retry_after():
    err = classify_google_error(http_error(429, headers={"retry-after": "9"}), "x")
    assert err.retry_after == 9


def test_google_socket_timeout_is_transient():
    assert isinstance(classify_google_error(TimeoutError(), "x"), TransientError)


def test_missing_oauth_client_file_is_config_error():
    with pytest.raises(ConfigError, match=r"credentials\.json"):
        GoogleDocsPublisher().credentials()


def test_expired_refresh_token_is_auth_error_with_reauth_hint(tmp_path, monkeypatch):
    (tmp_path / "token.json").write_text(json.dumps({
        "token": "old", "refresh_token": "r", "client_id": "c", "client_secret": "s",
        "token_uri": "https://oauth2.googleapis.com/token",
        "expiry": "2000-01-01T00:00:00Z", "scopes": SCOPES,
    }))

    def refuse(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refuse)
    with pytest.raises(AuthError) as info:
        GoogleDocsPublisher().credentials()
    assert "expired or been revoked" in info.value.message
    assert "Delete token.json" in info.value.hint
