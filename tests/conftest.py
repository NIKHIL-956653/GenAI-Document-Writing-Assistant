import json

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
