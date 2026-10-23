"""API clients against a fake transport: error classification, retries, timeouts."""
import pytest
import requests

from conftest import ISSUE, FakeTransport, make_response
from docgen.errors import (
    APIError,
    AuthError,
    InvalidTicketKeyError,
    RateLimitError,
    TicketNotFoundError,
    TransientError,
)
from docgen.jira import JiraClient
from docgen.llm.ollama import OllamaBackend
from docgen.llm.openrouter import OpenRouterBackend
from docgen.models import Ticket
from docgen.publishers.confluence import ConfluencePublisher

COMPLETION = {
    "choices": [{"message": {"content": "## Overview\n\nText"}}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
}


MESSAGES = [{"role": "user", "content": "write"}]


@pytest.fixture
def ticket():
    return Ticket(key="DEMO-2", title="Add login", description="Use JWT.")


# ── Jira ─────────────────────────────────────────────────────

def test_jira_fetch_parses_ticket(monkeypatch, jira_env):
    transport = FakeTransport(monkeypatch, make_response(200, ISSUE))
    ticket = JiraClient().get_ticket("demo-2")
    assert ticket == Ticket(key="DEMO-2", title="Add login", description="Use JWT.",
                            status="To Do", labels=["auth"])
    _, url, kwargs = transport.calls[0]
    assert url == "https://example.atlassian.net/rest/api/3/issue/DEMO-2"
    assert kwargs["timeout"][1] == 30.0  # every call carries a timeout


def test_jira_invalid_key_fails_before_any_request(monkeypatch, jira_env):
    transport = FakeTransport(monkeypatch)
    with pytest.raises(InvalidTicketKeyError, match="not a valid Jira issue key"):
        JiraClient().get_ticket("DEMO 2")
    assert transport.calls == []


def test_jira_404_is_ticket_not_found_and_not_retried(monkeypatch, jira_env, sleeps):
    transport = FakeTransport(monkeypatch, make_response(404, {"errorMessages": ["nope"]}))
    with pytest.raises(TicketNotFoundError, match="DEMO-9 was not found"):
        JiraClient().get_ticket("DEMO-9")
    assert len(transport.calls) == 1 and sleeps == []


def test_jira_401_is_auth_error_with_token_hint(monkeypatch, jira_env):
    FakeTransport(monkeypatch, make_response(401, text="Unauthorized"))
    with pytest.raises(AuthError) as info:
        JiraClient().get_ticket("DEMO-2")
    assert "JIRA_TOKEN" in info.value.hint


def test_jira_429_waits_for_retry_after_then_succeeds(monkeypatch, jira_env, sleeps):
    FakeTransport(monkeypatch,
                  make_response(429, headers={"Retry-After": "3"}),
                  make_response(200, ISSUE))
    assert JiraClient().get_ticket("DEMO-2").title == "Add login"
    assert 3 <= sleeps[0] <= 4


def test_jira_timeouts_and_5xx_are_retried(monkeypatch, jira_env, sleeps):
    transport = FakeTransport(monkeypatch,
                              requests.ReadTimeout(),
                              make_response(503, text="busy"),
                              make_response(200, ISSUE))
    JiraClient().get_ticket("DEMO-2")
    assert len(transport.calls) == 3 and len(sleeps) == 2


def test_jira_persistent_outage_raises_transient_error(monkeypatch, jira_env):
    FakeTransport(monkeypatch, *[requests.ConnectionError()] * 4)
    with pytest.raises(TransientError) as info:
        JiraClient().get_ticket("DEMO-2")
    assert "JIRA_DOMAIN" in info.value.hint


# ── OpenRouter ───────────────────────────────────────────────

def test_openrouter_returns_document_with_usage(monkeypatch, openrouter_env, ticket):
    transport = FakeTransport(monkeypatch, make_response(200, COMPLETION))
    completion = OpenRouterBackend().complete(MESSAGES)
    assert completion.text.startswith("## Overview")
    assert completion.usage.total_tokens == 150
    assert completion.usage.cost(1.0, 2.0) == pytest.approx(100 / 1e6 + 50 * 2 / 1e6)
    assert transport.calls[0][2]["headers"]["Authorization"] == "Bearer sk-test"


def test_openrouter_401_names_the_env_var(monkeypatch, openrouter_env, ticket):
    FakeTransport(monkeypatch, make_response(401, text="bad key"))
    with pytest.raises(AuthError) as info:
        OpenRouterBackend().complete(MESSAGES)
    assert "OPENROUTER_API_KEY" in info.value.hint


def test_openrouter_upstream_error_in_200_body_is_retried(monkeypatch, openrouter_env,
                                                          ticket, sleeps):
    FakeTransport(monkeypatch,
                  make_response(200, {"error": {"code": 502, "message": "provider down"}}),
                  make_response(200, COMPLETION))
    assert OpenRouterBackend().complete(MESSAGES).usage.total_tokens == 150
    assert len(sleeps) == 1


def test_openrouter_rate_limit_without_header_backs_off(monkeypatch, openrouter_env,
                                                        ticket, sleeps):
    FakeTransport(monkeypatch, *[make_response(429, text="slow down")] * 4)
    with pytest.raises(RateLimitError):
        OpenRouterBackend().complete(MESSAGES)
    assert len(sleeps) == 3


def test_openrouter_bad_request_is_not_retried(monkeypatch, openrouter_env, ticket, sleeps):
    FakeTransport(monkeypatch, make_response(400, text="bad model"))
    with pytest.raises(APIError):
        OpenRouterBackend().complete(MESSAGES)
    assert sleeps == []


# ── Ollama ───────────────────────────────────────────────────

def test_ollama_not_running_gives_actionable_hint(monkeypatch, ticket):
    FakeTransport(monkeypatch, *[requests.ConnectionError()] * 3)
    with pytest.raises(TransientError) as info:
        OllamaBackend().complete(MESSAGES)
    assert "ollama serve" in info.value.hint


def test_ollama_missing_model_says_to_pull(monkeypatch, ticket):
    FakeTransport(monkeypatch, make_response(404, {"error": "model not found"}))
    with pytest.raises(APIError) as info:
        OllamaBackend().complete(MESSAGES)
    assert "ollama pull" in info.value.hint


def test_ollama_usage_counts(monkeypatch, ticket):
    FakeTransport(monkeypatch, make_response(200, {
        "message": {"content": "doc"}, "prompt_eval_count": 10, "eval_count": 5}))
    assert OllamaBackend().complete(MESSAGES).usage.total_tokens == 15


# ── Confluence ───────────────────────────────────────────────

def test_confluence_missing_space_is_clear(monkeypatch, jira_env, ticket):
    from docgen.models import GeneratedDocument, TokenUsage
    doc = GeneratedDocument(ticket_key="DEMO-2", title="T", body="# x", backend="ollama",
                            usage=TokenUsage(model="m"))
    FakeTransport(monkeypatch, make_response(404, text="no space"))
    with pytest.raises(APIError) as info:
        ConfluencePublisher().publish(doc)
    assert "CONFLUENCE_SPACE_KEY" in info.value.hint
