import json
import threading

import pytest
import requests

from docgen import retry as retry_module

ENV_PREFIXES = ("JIRA_", "OPENROUTER_", "OLLAMA_", "CONFLUENCE_", "GOOGLE_")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """No real .env, no real credentials, no real sleeping."""
    monkeypatch.chdir(tmp_path)
    import os
    for name in list(os.environ):
        if name.startswith(ENV_PREFIXES):
            monkeypatch.delenv(name)
    sleeps = []
    monkeypatch.setattr(retry_module, "_sleep", sleeps.append)
    return sleeps


@pytest.fixture
def sleeps(isolated_env):
    return isolated_env


@pytest.fixture
def jira_env(monkeypatch):
    monkeypatch.setenv("JIRA_DOMAIN", "example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "secret")


def make_response(status=200, body=None, headers=None, text=None):
    r = requests.Response()
    r.status_code = status
    r._content = (text if text is not None else json.dumps(body or {})).encode()
    r.headers.update(headers or {})
    r.url = "https://example.test/"
    return r


class FakeTransport:
    """Replays queued responses/exceptions for requests.request and Session.request."""

    def __init__(self, monkeypatch, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        monkeypatch.setattr(requests, "request", self._request)
        monkeypatch.setattr(requests.Session, "request",
                            lambda _self, method, url, **kw: self._request(method, url, **kw))

    def _request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Router:
    """
    Routes requests by URL substring to queues of responses/exceptions, so one
    test can drive Jira, OpenRouter, Ollama and Confluence together. A route may
    also be a function (method, url, kwargs) -> response, for concurrent runs
    where the order of calls isn't fixed.
    """

    def __init__(self, monkeypatch, routes: dict):
        self.routes = {k: v if callable(v) else list(v) for k, v in routes.items()}
        self.calls = []
        self._lock = threading.Lock()
        monkeypatch.setattr(requests, "request", self._request)
        monkeypatch.setattr(requests.Session, "request",
                            lambda _self, method, url, **kw: self._request(method, url, **kw))

    def _request(self, method, url, **kwargs):
        with self._lock:
            self.calls.append((method, url, kwargs))
        for fragment, queue in self.routes.items():
            if fragment in url:
                if callable(queue):
                    outcome = queue(method, url, kwargs)
                    if isinstance(outcome, BaseException):
                        raise outcome
                    return outcome
                if not queue:
                    raise AssertionError(f"no more fake responses for {fragment}")
                outcome = queue.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
        raise AssertionError(f"unexpected request {method} {url}")

    def calls_to(self, fragment):
        return [c for c in self.calls if fragment in c[1]]


class FakeGoogleHttp:
    """
    Stands in for the httplib2 transport under googleapiclient. Responses are
    (status, body-dict, headers) tuples or exceptions, consumed in order.
    """

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def request(self, uri, method="GET", body=None, headers=None, **_):
        import httplib2
        try:
            parsed = json.loads(body) if body else None
        except (ValueError, UnicodeDecodeError):
            parsed = body  # multipart media upload
        self.calls.append({"uri": uri, "method": method, "body": parsed})
        if not self.outcomes:
            raise AssertionError(f"no more fake Google responses for {method} {uri}")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        status, payload, extra = (*outcome, {})[:3]
        resp = httplib2.Response({"status": status, "content-type": "application/json", **extra})
        return resp, json.dumps(payload).encode()


@pytest.fixture
def google_http(monkeypatch, tmp_path):
    """
    A valid token.json plus a FakeGoogleHttp wired into the publisher. Tests
    push outcomes with `google_http.outcomes.extend(...)`.
    """
    import datetime as dt

    from docgen.publishers import gdocs

    expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "token.json").write_text(json.dumps({
        "token": "access", "refresh_token": "refresh", "client_id": "cid", "client_secret": "cs",
        "token_uri": "https://oauth2.googleapis.com/token", "scopes": gdocs.SCOPES,
        "expiry": expiry,
    }))
    fake = FakeGoogleHttp()
    monkeypatch.setattr(gdocs, "AuthorizedHttp", lambda creds, http=None: fake)
    return fake


ISSUE = {
    "key": "DEMO-2",
    "fields": {
        "summary": "Add login",
        "description": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Use JWT."}]}]},
        "status": {"name": "To Do"},
        "labels": ["auth"],
    },
}

DOC_MARKDOWN = "## Overview\n\nSome **bold** text.\n\n- one\n- two\n"


def completion(text=DOC_MARKDOWN, cost=None):
    usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    if cost is not None:
        usage["cost"] = cost
    return make_response(200, {"choices": [{"message": {"content": text}}], "usage": usage})


def issue(key="DEMO-2", summary="Add login"):
    body = json.loads(json.dumps(ISSUE))
    body["key"], body["fields"]["summary"] = key, summary
    return make_response(200, body)


@pytest.fixture
def openrouter_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
