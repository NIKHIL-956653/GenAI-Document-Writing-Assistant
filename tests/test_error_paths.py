"""Every error branch in the clients, publishers and CLI that the happy-path tests don't reach."""
import datetime as dt
import json
import logging

import pytest
import requests

from conftest import FakeTransport, completion, make_response
from docgen.errors import (
    APIError,
    AuthError,
    ConfigError,
    RateLimitError,
    TicketNotFoundError,
    TransientError,
)
from docgen.jira import JiraClient
from docgen.llm.ollama import OllamaBackend
from docgen.llm.openrouter import OpenRouterBackend
from docgen.models import GeneratedDocument, TokenUsage
from docgen.publishers.confluence import ConfluencePublisher

MESSAGES = [{"role": "user", "content": "write"}]
DOC = GeneratedDocument(ticket_key="DEMO-2", title="T", body="## A\n\ntext", backend="x",
                        usage=TokenUsage(model="m"))


# ── Jira ─────────────────────────────────────────────────────

def test_jira_403_names_the_permission(monkeypatch, jira_env):
    FakeTransport(monkeypatch, make_response(403, text="forbidden"))
    with pytest.raises(AuthError) as info:
        JiraClient().get_ticket("DEMO-2")
    assert "Browse projects" in info.value.hint


@pytest.mark.parametrize("response", [
    make_response(200, text="<html>login page</html>"),
    make_response(200, {"key": "DEMO-2"}),                       # no fields
    make_response(200, {"key": "DEMO-2", "fields": {"summary": None}}),
])
def test_jira_malformed_response(monkeypatch, jira_env, response):
    FakeTransport(monkeypatch, response)
    with pytest.raises(APIError, match="unexpected Jira response"):
        JiraClient().get_ticket("DEMO-2")


def test_jira_other_4xx_is_plain_api_error(monkeypatch, jira_env, sleeps):
    FakeTransport(monkeypatch, make_response(400, text="bad jql"))
    with pytest.raises(APIError) as info:
        JiraClient().get_ticket("DEMO-2")
    assert info.value.status == 400 and not isinstance(info.value, TicketNotFoundError)
    assert sleeps == []


def test_invalid_jira_domain_setting(monkeypatch, jira_env):
    monkeypatch.setenv("JIRA_DOMAIN", "localhost")
    with pytest.raises(ConfigError, match=r"Invalid environment variable.*JIRA_DOMAIN"):
        JiraClient()


# ── OpenRouter ───────────────────────────────────────────────

def test_openrouter_402_says_add_credits(monkeypatch, openrouter_env):
    FakeTransport(monkeypatch, make_response(402, text="no credits"))
    with pytest.raises(APIError) as info:
        OpenRouterBackend().complete(MESSAGES)
    assert "credits" in info.value.hint


@pytest.mark.parametrize("bad", [
    make_response(200, text="<html>gateway</html>"),
    make_response(200, {"choices": []}),
    make_response(200, {"choices": [{"message": {"content": "   "}}]}),
])
def test_openrouter_malformed_bodies_are_retried(monkeypatch, openrouter_env, sleeps, bad):
    FakeTransport(monkeypatch, bad, completion())
    assert OpenRouterBackend().complete(MESSAGES).text.startswith("## Overview")
    assert len(sleeps) == 1


@pytest.mark.parametrize("error, expected", [
    ({"code": 429, "message": "upstream throttled"}, RateLimitError),
    ({"code": 400, "message": "context too long"}, APIError),
    ("plain string error", TransientError),
])
def test_openrouter_in_body_errors(monkeypatch, openrouter_env, error, expected):
    FakeTransport(monkeypatch, *[make_response(200, {"error": error})] * 4)
    with pytest.raises(expected):
        OpenRouterBackend().complete(MESSAGES)


def test_openrouter_reports_provider_cost(monkeypatch, openrouter_env):
    transport = FakeTransport(monkeypatch, completion(cost=0.0042))
    usage = OpenRouterBackend().complete(MESSAGES).usage
    assert usage.cost_usd == 0.0042 and usage.cost(100, 100) == 0.0042
    assert transport.calls[0][2]["json"]["usage"] == {"include": True}


# ── Ollama ───────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    make_response(200, text="not json"),
    make_response(200, {"message": {"content": ""}}),
])
def test_ollama_malformed_bodies(monkeypatch, bad):
    FakeTransport(monkeypatch, *[bad] * 3)
    with pytest.raises(TransientError):
        OllamaBackend().complete(MESSAGES)


def test_ollama_read_timeout_is_transient(monkeypatch):
    FakeTransport(monkeypatch, requests.ReadTimeout(), make_response(200, {"message": {"content": "ok"}}))
    assert OllamaBackend().complete(MESSAGES).text == "ok"


# ── Confluence ───────────────────────────────────────────────

@pytest.mark.parametrize("response, error, text", [
    (make_response(401, text="no"), AuthError, "JIRA_EMAIL"),
    (make_response(403, text="no"), AuthError, "Add page"),
    (make_response(400, text="A page with this title already exists"), APIError, "Rename"),
    (make_response(400, text="other"), APIError, None),
    (make_response(200, {"no": "links"}), APIError, None),
])
def test_confluence_errors(monkeypatch, jira_env, response, error, text):
    FakeTransport(monkeypatch, response)
    with pytest.raises(error) as info:
        ConfluencePublisher().publish(DOC)
    if text:
        assert text in info.value.hint


def test_confluence_space_from_env(monkeypatch, jira_env):
    monkeypatch.setenv("CONFLUENCE_SPACE_KEY", "DOCS")
    transport = FakeTransport(monkeypatch, make_response(200, {"_links": {"webui": "/x"}}))
    ConfluencePublisher().publish(DOC)
    assert transport.calls[0][2]["json"]["space"] == {"key": "DOCS"}


def test_storage_fallback_without_markdown_package(monkeypatch, caplog):
    import builtins

    from docgen.publishers.markdown_convert import markdown_to_storage
    real_import = builtins.__import__

    def no_markdown(name, *args, **kwargs):
        if name == "markdown":
            raise ImportError
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_markdown)
    assert markdown_to_storage("a <b>\n\nc") == "<p>a &lt;b&gt;</p><p>c</p>"


# ── Google credentials ───────────────────────────────────────

def token_json(path, expiry, scopes=True):
    from docgen.publishers.gdocs import SCOPES
    data = {"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
            "token_uri": "https://oauth2.googleapis.com/token", "expiry": expiry}
    if scopes:
        data["scopes"] = SCOPES
    path.write_text(json.dumps(data))


def future():
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeFlow:
    ran = False

    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        if "bad" in path:
            raise ValueError("Client secrets must be for a web or installed app.")
        return cls()

    def run_local_server(self, port, timeout_seconds):
        FakeFlow.ran = True
        from google.oauth2.credentials import Credentials
        return Credentials("new-token", refresh_token="r", client_id="c", client_secret="s",
                           token_uri="https://oauth2.googleapis.com/token")


@pytest.fixture
def flow(monkeypatch):
    from docgen.publishers import gdocs
    FakeFlow.ran = False
    monkeypatch.setattr(gdocs, "InstalledAppFlow", FakeFlow)
    return FakeFlow


def test_first_run_consents_and_saves_token(tmp_path, flow):
    from docgen.publishers.gdocs import GoogleDocsPublisher
    (tmp_path / "credentials.json").write_text("{}")
    creds = GoogleDocsPublisher().credentials()
    assert flow.ran and creds.token == "new-token"
    assert json.loads((tmp_path / "token.json").read_text())["token"] == "new-token"


def test_valid_token_is_reused_without_consent(tmp_path, flow):
    from docgen.publishers.gdocs import GoogleDocsPublisher
    token_json(tmp_path / "token.json", future())
    assert GoogleDocsPublisher().credentials().token == "t"
    assert not flow.ran


@pytest.mark.parametrize("scopes", [None, ["https://www.googleapis.com/auth/documents"]])
def test_token_without_all_scopes_triggers_consent(tmp_path, flow, scopes):
    from docgen.publishers.gdocs import GoogleDocsPublisher
    token_json(tmp_path / "token.json", future(), scopes=False)
    if scopes:
        data = json.loads((tmp_path / "token.json").read_text())
        (tmp_path / "token.json").write_text(json.dumps({**data, "scopes": scopes}))
    (tmp_path / "credentials.json").write_text("{}")
    GoogleDocsPublisher().credentials()
    assert flow.ran


def test_unreadable_token_file_is_ignored(tmp_path, flow, caplog):
    from docgen.publishers.gdocs import GoogleDocsPublisher
    (tmp_path / "token.json").write_text('{"token": "only"}')
    (tmp_path / "credentials.json").write_text("{}")
    with caplog.at_level(logging.WARNING):
        GoogleDocsPublisher().credentials()
    assert flow.ran


def test_invalid_client_file_is_config_error(tmp_path, flow, monkeypatch):
    from docgen.publishers.gdocs import GoogleDocsPublisher
    monkeypatch.setenv("GOOGLE_CREDENTIALS_FILE", str(tmp_path / "bad.json"))
    (tmp_path / "bad.json").write_text("{}")
    with pytest.raises(ConfigError, match="not a valid OAuth client file"):
        GoogleDocsPublisher().credentials()


def test_expired_token_refreshes_silently(tmp_path, monkeypatch, flow):
    from docgen.publishers.gdocs import GoogleDocsPublisher
    token_json(tmp_path / "token.json", "2000-01-01T00:00:00Z")

    def refresh(self, request):
        self.token = "refreshed"
        self.expiry = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=1)

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refresh)
    assert GoogleDocsPublisher().credentials().token == "refreshed"
    assert json.loads((tmp_path / "token.json").read_text())["token"] == "refreshed"
    assert not flow.ran


def test_refresh_network_failure_is_transient(tmp_path, monkeypatch):
    from google.auth.exceptions import TransportError as GoogleTransportError

    from docgen.publishers.gdocs import GoogleDocsPublisher
    token_json(tmp_path / "token.json", "2000-01-01T00:00:00Z")

    def fail(self, request):
        raise GoogleTransportError("connection reset")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fail)
    with pytest.raises(TransientError, match="refresh the OAuth token"):
        GoogleDocsPublisher().credentials()


def test_token_revoked_mid_call_is_auth_error(google_http, jira_env, openrouter_env, monkeypatch):
    from google.auth.exceptions import RefreshError

    from docgen.publishers.gdocs import GoogleDocsPublisher
    google_http.outcomes.append(RefreshError("invalid_grant"))
    with pytest.raises(AuthError) as info:
        GoogleDocsPublisher().publish(DOC)
    assert "sign in again" in info.value.hint


def test_google_401_mid_call_is_auth_error(google_http):
    from docgen.publishers.gdocs import GoogleDocsPublisher
    google_http.outcomes.append((401, {"error": {"code": 401, "message": "invalid creds"}}))
    with pytest.raises(AuthError):
        GoogleDocsPublisher().publish(DOC)


def test_empty_document_skips_the_write(google_http):
    from docgen.publishers.gdocs import GoogleDocsPublisher
    google_http.outcomes.append((200, {"documentId": "d", "revisionId": "r"}))
    empty = DOC.model_copy(update={"body": "   "})
    assert GoogleDocsPublisher().publish(empty) == "https://docs.google.com/document/d/d"
    assert len(google_http.calls) == 1


# ── CLI / pipeline edges ─────────────────────────────────────

def test_keyboard_interrupt_exit_130(jira_env, openrouter_env, monkeypatch):
    from docgen import cli
    monkeypatch.setattr(cli.Pipeline, "run", lambda self, t: (_ for _ in ()).throw(KeyboardInterrupt))
    assert cli.main(["DEMO-2"]) == 130


def test_eval_keyboard_interrupt_exit_130(openrouter_env, monkeypatch):
    import docgen.eval as ev
    monkeypatch.setattr(ev, "run_eval", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    from docgen.cli import main
    assert main(["eval", "--only", "login-jwt"]) == 130


def test_review_eof_declines(jira_env, monkeypatch):
    from docgen.pipeline import confirm
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert confirm("publish?") is False


def test_templates_command_reports_broken_user_template(tmp_path, capsys):
    from docgen.cli import main
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "broken.yaml").write_text("title: [")
    assert main(["templates"]) == 2
    assert "broken.yaml is not valid YAML" in capsys.readouterr().err


def test_unknown_backend_and_publisher_names():
    from docgen.llm import get_backend
    from docgen.publishers import get_publisher
    with pytest.raises(ValueError):
        get_backend("gpt")
    with pytest.raises(ValueError):
        get_publisher("notion")


def test_preflight_eval_judge_backend_settings(monkeypatch):
    from docgen import config
    config.preflight_backends(["ollama"])  # local model: nothing required
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        config.preflight_backends(["ollama", "openrouter"])


def test_preflight_confluence_and_bad_google_setting(jira_env, openrouter_env, monkeypatch):
    from docgen import config
    config.preflight("openrouter", "confluence")
    monkeypatch.setenv("GOOGLE_TIMEOUT", "soon")
    with pytest.raises(ConfigError, match="GOOGLE_TIMEOUT"):
        config.preflight("openrouter", "gdocs")


# ── logging ──────────────────────────────────────────────────

def test_text_and_json_formatters_render_context_and_exceptions():
    from docgen.logging_setup import JsonFormatter, TextFormatter
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        exc_info = sys.exc_info()
    record = logging.LogRecord("docgen.x", logging.ERROR, __file__, 1, "failed", None, exc_info)
    record.ticket, record.title, record.cost, record.empty = "DEMO-2", "two words", 0.123456789, ""
    text = TextFormatter().format(record)
    assert "failed  ticket=DEMO-2 title='two words' cost=0.123457 empty=''" in text
    assert "ValueError: boom" in text
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "failed" and payload["ticket"] == "DEMO-2"
    assert "ValueError: boom" in payload["exc_info"]


# ── http helpers ─────────────────────────────────────────────

def test_retry_after_http_date_in_the_future():
    from email.utils import format_datetime

    from docgen.http import parse_retry_after
    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)
    assert 25 < parse_retry_after(format_datetime(when, usegmt=True)) <= 30
    # dates without a zone are treated as UTC
    naive = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S")
    assert 25 < parse_retry_after(naive) <= 30


def test_long_error_bodies_are_truncated(monkeypatch, jira_env):
    FakeTransport(monkeypatch, make_response(400, text="x" * 1000))
    with pytest.raises(APIError) as info:
        JiraClient().get_ticket("DEMO-2")
    assert info.value.message.endswith("…") and len(info.value.message) < 400
