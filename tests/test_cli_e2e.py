"""
The whole pipeline through the real CLI entry point, with Jira, OpenRouter,
Ollama, Confluence and the Google APIs replaced by fakes at the transport
layer — so retries, error classification and exit codes are exercised for
real, with no network.
"""
import json

import httplib2
import pytest
import requests

from conftest import Router, completion, issue, make_response
from docgen.cli import main
from docgen.publishers.gdocs import SCOPES

JIRA = "atlassian.net/rest/api"
OPENROUTER = "openrouter.ai"
CONFLUENCE = "atlassian.net/wiki"


@pytest.fixture
def env(jira_env, openrouter_env):
    pass


def google_ok(doc_id="doc123"):
    return [(200, {"documentId": doc_id, "revisionId": "rev1"}),   # documents.create
            (200, {"replies": []})]                                 # documents.batchUpdate


# ── happy paths ──────────────────────────────────────────────

def test_generate_only_writes_markdown(env, monkeypatch, tmp_path):
    router = Router(monkeypatch, {JIRA: [issue()], OPENROUTER: [completion()]})
    assert main(["DEMO-2"]) == 0
    assert (tmp_path / "DEMO-2_document.md").read_text().startswith("# Add login\n\n## Overview")
    prompt = router.calls_to(OPENROUTER)[0][2]["json"]["messages"][1]["content"]
    assert "## Known Limitations" in prompt  # default technical template


def test_full_pipeline_to_google_docs_with_template(env, monkeypatch, tmp_path, google_http):
    google_http.outcomes += [*google_ok(), (200, {"id": "perm"})]
    router = Router(monkeypatch, {JIRA: [issue()], OPENROUTER: [completion()]})

    assert main(["DEMO-2", "--template", "runbook", "--publish", "gdocs", "--public"]) == 0

    assert (tmp_path / "DEMO-2_runbook_document.md").exists()
    prompt = router.calls_to(OPENROUTER)[0][2]["json"]["messages"]
    assert "site reliability engineer" in prompt[0]["content"]
    assert "## Rollback" in prompt[1]["content"]

    create, write, share = google_http.calls
    assert create["body"] == {"title": "Add login"}
    assert write["body"]["writeControl"] == {"requiredRevisionId": "rev1"}
    assert write["body"]["requests"][0]["insertText"]["text"].startswith("Overview\n")
    assert "doc123/permissions" in share["uri"]
    assert share["body"] == {"type": "anyone", "role": "reader"}


def test_private_by_default_no_share_call(env, monkeypatch, google_http):
    google_http.outcomes += google_ok()
    Router(monkeypatch, {JIRA: [issue()], OPENROUTER: [completion()]})
    assert main(["DEMO-2", "--publish", "gdocs"]) == 0
    assert len(google_http.calls) == 2


def test_full_pipeline_to_confluence(env, monkeypatch, capsys):
    router = Router(monkeypatch, {
        JIRA: [issue()], OPENROUTER: [completion()],
        CONFLUENCE: [make_response(200, {"id": "1", "_links": {"webui": "/spaces/KAN/pages/1"}})],
    })
    assert main(["DEMO-2", "--publish", "confluence"]) == 0
    body = router.calls_to(CONFLUENCE)[0][2]["json"]
    assert body["space"] == {"key": "KAN"}
    assert "<h2>Overview</h2>" in body["body"]["storage"]["value"]
    assert "https://example.atlassian.net/wiki/spaces/KAN/pages/1" in capsys.readouterr().err


def test_ollama_backend_end_to_end(jira_env, monkeypatch, tmp_path):
    Router(monkeypatch, {JIRA: [issue()], "localhost:11434": [make_response(200, {
        "message": {"content": "## Overview\n\ntext"}, "prompt_eval_count": 5, "eval_count": 7})]})
    assert main(["DEMO-2", "--llm", "ollama"]) == 0
    assert (tmp_path / "DEMO-2_document.md").exists()


def test_json_logs_carry_run_summary(env, monkeypatch, capsys):
    Router(monkeypatch, {JIRA: [issue()], OPENROUTER: [completion(cost=0.0012)]})
    assert main(["DEMO-2", "--log-format", "json"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    summary = next(e for e in events if e["event"] == "run summary")
    assert summary["succeeded"] == 1
    assert summary["total_cost_usd"] == 0.0012  # provider-reported cost wins over the estimate


# ── batch behaviour ──────────────────────────────────────────

def test_batch_continues_past_a_missing_ticket(env, monkeypatch, tmp_path):
    Router(monkeypatch, {
        JIRA: [issue("DEMO-1"), make_response(404, text="nope"), issue("DEMO-3")],
        OPENROUTER: [completion(), completion()],
    })
    assert main(["DEMO-1", "DEMO-2", "DEMO-3"]) == 1
    assert (tmp_path / "DEMO-1_document.md").exists()
    assert not (tmp_path / "DEMO-2_document.md").exists()
    assert (tmp_path / "DEMO-3_document.md").exists()


def test_jira_auth_failure_aborts_batch_with_exit_2(env, monkeypatch, capsys):
    router = Router(monkeypatch, {JIRA: [make_response(401, text="bad")]})
    assert main(["DEMO-1", "DEMO-2"]) == 2
    assert len(router.calls) == 1
    assert "JIRA_TOKEN" in capsys.readouterr().err


def test_rate_limited_llm_recovers(env, monkeypatch, sleeps):
    Router(monkeypatch, {JIRA: [issue()],
                         OPENROUTER: [make_response(429, headers={"Retry-After": "2"}),
                                      make_response(503, text="busy"), completion()]})
    assert main(["DEMO-2"]) == 0
    assert len(sleeps) == 2 and 2 <= sleeps[0] <= 3


def test_llm_outage_fails_ticket_but_not_run(env, monkeypatch):
    Router(monkeypatch, {JIRA: [issue()], OPENROUTER: [requests.ReadTimeout()] * 4})
    assert main(["DEMO-2"]) == 1


# ── Google error paths ───────────────────────────────────────

def test_google_rate_limit_on_create_is_retried(env, monkeypatch, google_http, sleeps):
    google_http.outcomes += [(429, {"error": {"code": 429, "message": "slow"}}, {"retry-after": "1"}),
                             *google_ok()]
    Router(monkeypatch, {JIRA: [issue()], OPENROUTER: [completion()]})
    assert main(["DEMO-2", "--publish", "gdocs"]) == 0
    assert len(google_http.calls) == 3 and 1 <= sleeps[0] <= 2


def test_google_timeout_on_write_retries_only_the_write(env, monkeypatch, google_http):
    google_http.outcomes += [(200, {"documentId": "d", "revisionId": "r"}), TimeoutError(),
                             (200, {"replies": []})]
    Router(monkeypatch, {JIRA: [issue()], OPENROUTER: [completion()]})
    assert main(["DEMO-2", "--publish", "gdocs"]) == 0
    assert [c["uri"].split("?")[0].rsplit("/", 1)[-1] for c in google_http.calls] == \
        ["documents", "d:batchUpdate", "d:batchUpdate"]  # one create, never a second doc


def test_google_share_failure_keeps_local_doc(env, monkeypatch, tmp_path, google_http):
    google_http.outcomes += [*google_ok(), (400, {"error": {"code": 400, "message": "bad"}})]
    Router(monkeypatch, {JIRA: [issue()], OPENROUTER: [completion()]})
    assert main(["DEMO-2", "--publish", "gdocs", "--public"]) == 1
    assert (tmp_path / "DEMO-2_document.md").exists()


def test_google_api_disabled_is_a_setup_error(env, monkeypatch, google_http, capsys):
    google_http.outcomes += [(403, {"error": {"code": 403, "message": "Docs API has not been used",
                                              "errors": [{"reason": "accessNotConfigured"}]}})]
    Router(monkeypatch, {JIRA: [issue()], OPENROUTER: [completion()]})
    assert main(["DEMO-2", "--publish", "gdocs"]) == 2
    assert "Enable the Google Docs API" in capsys.readouterr().err


def test_google_dns_failure_retries_then_fails_ticket(env, monkeypatch, google_http):
    google_http.outcomes += [httplib2.ServerNotFoundError("dns")] * 4
    Router(monkeypatch, {JIRA: [issue()], OPENROUTER: [completion()]})
    assert main(["DEMO-2", "--publish", "gdocs"]) == 1
    assert len(google_http.calls) == 4


def test_expired_google_token_is_exit_2_with_reauth_hint(env, monkeypatch, tmp_path, capsys):
    from google.auth.exceptions import RefreshError
    (tmp_path / "token.json").write_text(json.dumps({
        "token": "old", "refresh_token": "r", "client_id": "c", "client_secret": "s",
        "token_uri": "https://oauth2.googleapis.com/token", "expiry": "2000-01-01T00:00:00Z",
        "scopes": SCOPES}))

    def refuse(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refuse)
    Router(monkeypatch, {JIRA: [issue()], OPENROUTER: [completion()]})
    assert main(["DEMO-2", "--publish", "gdocs"]) == 2
    err = capsys.readouterr().err
    assert "expired or been revoked" in err and "Delete token.json" in err
    assert (tmp_path / "DEMO-2_document.md").exists()  # generation was not lost


# ── review gate ──────────────────────────────────────────────

@pytest.mark.parametrize("answer, published", [("y", True), ("n", False)])
def test_review_gate(env, monkeypatch, google_http, capsys, answer, published):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    google_http.outcomes += google_ok()
    Router(monkeypatch, {JIRA: [issue()], OPENROUTER: [completion()]})
    assert main(["DEMO-2", "--publish", "gdocs", "--review"]) == 0
    assert "## Overview" in capsys.readouterr().out  # document shown before asking
    assert bool(google_http.calls) is published


# ── setup errors ─────────────────────────────────────────────

def test_unknown_template_is_exit_2(env, capsys):
    assert main(["DEMO-2", "--template", "nope"]) == 2
    assert "Available: api_docs" in capsys.readouterr().err


def test_missing_confluence_space_is_clear(env, monkeypatch, capsys):
    Router(monkeypatch, {JIRA: [issue()], OPENROUTER: [completion()],
                         CONFLUENCE: [make_response(404, text="no space")]})
    assert main(["DEMO-2", "--publish", "confluence"]) == 1
    assert "CONFLUENCE_SPACE_KEY" in capsys.readouterr().err


def test_main_module_entry_point(env, monkeypatch):
    import runpy
    Router(monkeypatch, {JIRA: [issue()], OPENROUTER: [completion()]})
    monkeypatch.setattr("sys.argv", ["docgen", "DEMO-2"])
    with pytest.raises(SystemExit) as info:
        runpy.run_module("docgen", run_name="__main__")
    assert info.value.code == 0


def test_templates_command_lists_builtins(capsys):
    assert main(["templates"]) == 0
    out = capsys.readouterr().out
    for name in ("technical", "prd", "runbook", "release_notes", "api_docs", "postmortem"):
        assert name in out
