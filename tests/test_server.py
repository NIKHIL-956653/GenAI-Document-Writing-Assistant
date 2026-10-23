"""The HTTP API and Jira webhook, through FastAPI's TestClient, with Jira/LLM/Google faked."""
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from conftest import Router, completion, issue, make_response
from docgen.config import DocgenSettings
from docgen.server import create_app, webhook_decision
from docgen.store import Store

TOKEN = "api-token"
SECRET = "hook-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def env(jira_env, openrouter_env, monkeypatch, tmp_path):
    monkeypatch.setenv("DOCGEN_API_TOKEN", TOKEN)
    monkeypatch.setenv("DOCGEN_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("DOCGEN_STORE", str(tmp_path / "server.db"))
    monkeypatch.setenv("DOCGEN_OUTPUT_DIR", str(tmp_path / "docs"))
    (tmp_path / "docs").mkdir()


def jira_and_llm(monkeypatch, llm=None):
    def jira(method, url, kwargs):
        if url.endswith("/search/jql"):
            return make_response(200, {"issues": [{"key": "DEMO-7"}, {"key": "DEMO-8"}], "isLast": True})
        key = url.rsplit("/", 1)[-1]
        return make_response(404, text="") if key == "DEMO-404" else issue(key)
    return Router(monkeypatch, {"atlassian.net": jira,
                                "openrouter.ai": llm or (lambda m, u, k: completion())})


@pytest.fixture
def client(env, monkeypatch):
    jira_and_llm(monkeypatch)
    with TestClient(create_app()) as c:
        yield c


def wait(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/status/{job_id}", headers=AUTH).json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def signed(payload):
    body = json.dumps(payload).encode()
    return body, {"X-Hub-Signature": "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
                  "Content-Type": "application/json"}


def transition(key="DEMO-4", to="Done", change_id="100"):
    return {"webhookEvent": "jira:issue_updated", "issue": {"key": key},
            "changelog": {"id": change_id, "items": [
                {"field": "assignee", "toString": "Sam"},
                {"field": "status", "fieldId": "status", "fromString": "In Progress", "toString": to}]}}


# ── basics and auth ──────────────────────────────────────────

def test_healthz_needs_no_auth(client):
    assert client.get("/healthz").json() == {"status": "ok", "version": "0.2.0"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}])
def test_api_requires_the_bearer_token(client, headers):
    r = client.get("/jobs", headers=headers)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_open_api_when_no_token_configured(jira_env, openrouter_env, tmp_path, monkeypatch, caplog):
    jira_and_llm(monkeypatch)
    settings = DocgenSettings(store=tmp_path / "s.db", output_dir=tmp_path)
    with TestClient(create_app(settings)) as c:
        assert c.get("/jobs").status_code == 200
    assert "DOCGEN_API_TOKEN is not set" in caplog.text


def test_templates_endpoint(client):
    names = [t["name"] for t in client.get("/templates", headers=AUTH).json()]
    assert names == ["api_docs", "postmortem", "prd", "release_notes", "runbook", "technical"]


# ── generate / publish / status ──────────────────────────────

def test_generate_job_lifecycle(client, tmp_path):
    r = client.post("/generate", json={"tickets": ["demo-2", "DEMO-3"], "template": "prd"}, headers=AUTH)
    assert r.status_code == 202
    accepted = r.json()
    assert accepted["status"] == "queued" and accepted["status_url"] == f"/status/{accepted['job_id']}"

    job = wait(client, accepted["job_id"])
    assert job["status"] == "succeeded" and job["kind"] == "generate"
    assert [(x["ticket"], x["action"]) for x in job["results"]] == [("DEMO-2", "saved"), ("DEMO-3", "saved")]
    assert (tmp_path / "docs" / "DEMO-2_prd_document.md").exists()

    docs = client.get("/documents", headers=AUTH).json()
    assert {d["ticket_key"] for d in docs} == {"DEMO-2", "DEMO-3"} and "markdown" not in docs[0]
    one = client.get("/documents", params={"ticket": "demo-2", "include_markdown": True}, headers=AUTH).json()
    assert len(one) == 1 and one[0]["markdown"].startswith("# Add login")
    assert client.get("/jobs", headers=AUTH).json()[0]["id"] == accepted["job_id"]


def test_second_identical_request_is_incremental(client):
    def run():
        accepted = client.post("/generate", json={"tickets": ["DEMO-2"]}, headers=AUTH).json()
        return wait(client, accepted["job_id"])

    first, second = run(), run()
    assert first["results"][0]["action"] == "saved" and second["results"][0]["action"] == "unchanged"


def test_generate_with_jql(client):
    job = wait(client, client.post("/generate", json={"jql": "project = DEMO", "tickets": ["DEMO-7"]},
                                   headers=AUTH).json()["job_id"])
    assert [x["ticket"] for x in job["results"]] == ["DEMO-7", "DEMO-8"]


def test_failed_ticket_marks_job_failed(client):
    job = wait(client, client.post("/generate", json={"tickets": ["DEMO-2", "DEMO-404"]},
                                   headers=AUTH).json()["job_id"])
    assert job["status"] == "failed" and "see results" in job["error"]
    assert [x["ok"] for x in job["results"]] == [True, False]


def test_publish_job_uses_google_without_a_browser(client, google_http, tmp_path):
    google_http.outcomes += [(200, {"documentId": "d1", "revisionId": "r"}), (200, {"replies": []})]
    job = wait(client, client.post("/publish", json={"tickets": ["DEMO-2"], "target": "gdocs"},
                                   headers=AUTH).json()["job_id"])
    assert job["status"] == "succeeded", job
    assert job["results"][0]["url"] == "https://docs.google.com/document/d/d1"


def test_publish_without_google_token_fails_clearly(client, tmp_path):
    (tmp_path / "credentials.json").write_text("{}")   # passes preflight, but no token
    job = wait(client, client.post("/publish", json={"tickets": ["DEMO-2"], "target": "gdocs"},
                                   headers=AUTH).json()["job_id"])
    assert job["status"] == "failed" and "docgen auth google" in job["error"]


@pytest.mark.parametrize("body, status, message", [
    ({}, 422, "give `tickets`, `jql`, or both"),
    ({"tickets": ["bad key"]}, 422, "not a valid Jira issue key"),
    ({"tickets": ["DEMO-2"], "llm": "gpt"}, 422, "openrouter"),
    ({"tickets": ["DEMO-2"], "template": "nope"}, 400, "Unknown template"),
    ({"tickets": ["DEMO-2"], "max_results": 0}, 422, "greater than or equal to 1"),
])
def test_request_validation(client, body, status, message):
    r = client.post("/generate", json=body, headers=AUTH)
    assert r.status_code == status and message in r.text


def test_misconfigured_server_is_503(client, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    r = client.post("/generate", json={"tickets": ["DEMO-2"]}, headers=AUTH)
    assert r.status_code == 503 and "OPENROUTER_API_KEY" in r.json()["detail"]


def test_unknown_job_is_404(client):
    assert client.get("/status/nope", headers=AUTH).status_code == 404


def test_documents_rejects_bad_key(client):
    assert client.get("/documents", params={"ticket": "x y"}, headers=AUTH).status_code == 422


def test_job_setup_error_and_crash_are_recorded(client, monkeypatch):
    from docgen import server
    from docgen.errors import ConfigError

    def boom(self, tickets):
        raise ConfigError("no luck")

    monkeypatch.setattr(server.Pipeline, "run", boom)
    job = wait(client, client.post("/generate", json={"tickets": ["DEMO-2"]}, headers=AUTH).json()["job_id"])
    assert job["status"] == "failed" and job["error"] == "no luck"

    monkeypatch.setattr(server.Pipeline, "run", lambda self, t: 1 / 0)
    job = wait(client, client.post("/generate", json={"tickets": ["DEMO-2"]}, headers=AUTH).json()["job_id"])
    assert job["status"] == "failed" and "ZeroDivisionError" in job["error"]


def test_unfinished_jobs_are_marked_interrupted_at_startup(env, monkeypatch, tmp_path, caplog):
    store = Store(tmp_path / "server.db")
    stale, _ = store.create_job("generate", {"tickets": ["DEMO-2"]})
    jira_and_llm(monkeypatch)
    with TestClient(create_app()) as c:
        assert c.get(f"/status/{stale.id}", headers=AUTH).json()["status"] == "interrupted"
    assert "marked unfinished jobs" in caplog.text


# ── webhook ──────────────────────────────────────────────────

def test_webhook_with_valid_signature_queues_a_job(client, tmp_path):
    body, headers = signed(transition())
    r = client.post("/webhooks/jira", content=body, headers=headers)
    assert r.status_code == 202
    job = wait(client, r.json()["job_id"])
    assert job["kind"] == "webhook" and job["results"][0]["ticket"] == "DEMO-4"


def test_webhook_retries_are_deduplicated(client):
    body, headers = signed(transition())
    first = client.post("/webhooks/jira", content=body, headers=headers).json()
    again = client.post("/webhooks/jira", content=body, headers=headers)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert again.json()["job_id"] == first["job_id"]
    # Jira's delivery id takes precedence when present
    body2, headers2 = signed(transition(change_id="200"))
    headers2["X-Atlassian-Webhook-Identifier"] = "delivery-1"
    a = client.post("/webhooks/jira", content=body2, headers=headers2).json()
    b = client.post("/webhooks/jira", content=body2, headers=headers2).json()
    assert a["job_id"] == b["job_id"] and b["duplicate"]


@pytest.mark.parametrize("headers, query", [
    ({"X-Hub-Signature": "sha256=deadbeef"}, ""),
    ({"X-Docgen-Webhook-Token": "wrong"}, ""),
    ({}, "?token=wrong"),
    ({}, ""),
])
def test_webhook_rejects_bad_credentials(client, headers, query):
    body = json.dumps(transition()).encode()
    assert client.post(f"/webhooks/jira{query}", content=body, headers=headers).status_code == 401


@pytest.mark.parametrize("headers, query", [({"X-Docgen-Webhook-Token": SECRET}, ""),
                                            ({}, f"?token={SECRET}")])
def test_webhook_accepts_shared_token(client, headers, query):
    body = json.dumps(transition(change_id=query or "h")).encode()
    assert client.post(f"/webhooks/jira{query}", content=body, headers=headers).status_code == 202


def test_webhook_disabled_without_secret(env, monkeypatch):
    monkeypatch.delenv("DOCGEN_WEBHOOK_SECRET")
    jira_and_llm(monkeypatch)
    with TestClient(create_app()) as c:
        r = c.post("/webhooks/jira", content=b"{}")
    assert r.status_code == 503 and "DOCGEN_WEBHOOK_SECRET" in r.text


def test_webhook_rejects_non_json(client):
    body = b"not json"
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/jira", content=body, headers={"X-Hub-Signature": sig}).status_code == 400


@pytest.mark.parametrize("payload, reason", [
    ({"webhookEvent": "jira:issue_updated"}, "no issue key"),
    ({"issue": {"key": "not valid"}}, "invalid issue key"),
    ({"issue": {"key": "DEMO-4"}, "changelog": {"items": [{"field": "summary", "toString": "x"}]}},
     "no status change"),
    (transition(to="In Review"), "not one of the configured webhook statuses"),
])
def test_webhook_ignores_irrelevant_events(client, payload, reason):
    body, headers = signed(payload)
    r = client.post("/webhooks/jira", content=body, headers=headers)
    assert r.status_code == 200 and r.json()["action"] == "ignored" and reason in r.json()["reason"]


def test_webhook_statuses_are_configurable_and_case_insensitive(env, monkeypatch):
    monkeypatch.setenv("DOCGEN_WEBHOOK_STATUSES", "Ready for Release, Done")
    monkeypatch.setenv("DOCGEN_WEBHOOK_TEMPLATE", "release_notes")
    jira_and_llm(monkeypatch)
    with TestClient(create_app()) as c:
        body, headers = signed(transition(to="ready FOR release"))
        r = c.post("/webhooks/jira", content=body, headers=headers)
        assert r.status_code == 202
        job = wait(c, r.json()["job_id"])
    assert job["request"]["template"] == "release_notes"


def test_webhook_can_publish(env, monkeypatch, google_http):
    monkeypatch.setenv("DOCGEN_WEBHOOK_PUBLISH", "gdocs")
    google_http.outcomes += [(200, {"documentId": "w1", "revisionId": "r"}), (200, {"replies": []})]
    jira_and_llm(monkeypatch)
    with TestClient(create_app()) as c:
        body, headers = signed(transition())
        job = wait(c, c.post("/webhooks/jira", content=body, headers=headers).json()["job_id"])
    assert job["request"]["target"] == "gdocs" and job["results"][0]["action"] == "created"


def test_workflow_post_function_payload():
    decision = webhook_decision({"issue": {"key": "DEMO-4"}, "transition": {"to_status": "Done"},
                                 "timestamp": 123}, {"done"})
    assert decision == {"action": "queue", "ticket": "DEMO-4", "status": "Done", "change_id": "123"}


@pytest.mark.parametrize("name, value", [("DOCGEN_LLM", "gpt"), ("DOCGEN_WEBHOOK_PUBLISH", "notion")])
def test_invalid_server_settings(monkeypatch, name, value):
    from docgen.config import load
    from docgen.errors import ConfigError
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        load(DocgenSettings)


# ── CLI entry points ─────────────────────────────────────────

def test_serve_command_runs_uvicorn(env, monkeypatch):
    import uvicorn

    from docgen.cli import main
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    assert main(["serve", "--port", "9999"]) == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 9999 and seen["app"].title == "docgen"


def test_serve_command_without_server_extras(monkeypatch, capsys):
    import builtins

    from docgen.cli import main
    real_import = builtins.__import__

    def no_fastapi(name, *args, **kwargs):
        if name in ("uvicorn", "docgen.server"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_fastapi)
    assert main(["serve"]) == 2
    assert "docgen[server]" in capsys.readouterr().err


def test_serve_command_bad_settings(monkeypatch):
    from docgen.cli import main
    monkeypatch.setenv("DOCGEN_WORKERS", "0")
    assert main(["serve"]) == 2


def test_auth_google_command(tmp_path, monkeypatch, capsys):
    from docgen.cli import main
    from docgen.publishers.gdocs import GoogleDocsPublisher
    monkeypatch.setattr(GoogleDocsPublisher, "credentials", lambda self: None)
    assert main(["auth", "google"]) == 0
    assert "token.json" in capsys.readouterr().out


def test_auth_google_command_errors(tmp_path, monkeypatch):
    from docgen.cli import main
    from docgen.errors import TransientError
    assert main(["auth", "google"]) == 2  # no credentials.json
    from docgen.publishers.gdocs import GoogleDocsPublisher

    def offline(self):
        raise TransientError("offline", service="google")

    monkeypatch.setattr(GoogleDocsPublisher, "credentials", offline)
    assert main(["auth", "google"]) == 1
