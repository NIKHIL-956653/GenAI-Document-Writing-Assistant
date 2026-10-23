"""
Batch + incremental behaviour end to end through the CLI: JQL, concurrency,
the SQLite store, in-place updates of Google Docs and Confluence pages, diffs,
and image embedding — with every external API faked.
"""
import json
import threading

import pytest

from conftest import Router, completion, make_response
from docgen.cli import main
from docgen.store import Store

JIRA = "atlassian.net/rest/api"
OPENROUTER = "openrouter.ai"
CONFLUENCE = "atlassian.net/wiki"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64

DOC_V1 = "## Overview\n\nFirst version of the overview.\n\n## Known Limitations\n\n- none yet\n"
DOC_V2 = ("## Overview\n\nSecond version of the overview.\n\n## Rollout\n\nBehind a flag.\n\n"
          "## Known Limitations\n\n- none yet\n")


@pytest.fixture
def env(jira_env, openrouter_env):
    pass


class FakeJira:
    """Serves issues from a dict that tests can edit between runs."""

    def __init__(self):
        self.issues = {}
        self.fail = {}

    def add(self, key, summary="Add login", description="Use JWT.", **fields):
        self.issues[key] = {"key": key, "fields": {
            "summary": summary, "status": {"name": "To Do"}, "labels": [],
            "description": {"type": "doc", "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": description}]}]}, **fields}}

    def __call__(self, method, url, kwargs):
        if url.endswith("/search/jql"):
            keys = sorted(self.issues)
            return make_response(200, {"issues": [{"key": k} for k in keys], "isLast": True})
        if "/attachment/content/" in url:
            r = make_response(200, text="")
            r._content, r._content_consumed = PNG, True
            return r
        key = url.rsplit("/", 1)[-1]
        if key in self.fail:
            return self.fail[key]
        return make_response(200, self.issues[key]) if key in self.issues else make_response(404, text="")


class FakeLLM:
    def __init__(self, text=DOC_V1):
        self.text = text
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, method, url, kwargs):
        with self._lock:
            self.calls += 1
        return completion(self.text)


@pytest.fixture
def jira():
    j = FakeJira()
    j.add("DEMO-2")
    return j


@pytest.fixture
def llm():
    return FakeLLM()


@pytest.fixture
def services(monkeypatch, jira, llm):
    return Router(monkeypatch, {JIRA: jira, OPENROUTER: llm})


def store(tmp_path):
    return Store(tmp_path / ".docgen" / "docgen.db")


def google_create(doc_id="doc1"):
    return [(200, {"documentId": doc_id, "revisionId": "r1"}), (200, {"replies": []})]


def google_update(doc_id="doc1", title="Add login", trashed=False, end_index=60):
    return [(200, {"id": doc_id, "trashed": trashed}),
            (200, {"documentId": doc_id, "title": title, "revisionId": "r7",
                   "body": {"content": [{"endIndex": 1}, {"endIndex": end_index}]}}),
            (200, {"replies": []})]


def api_names(calls):
    out = []
    for c in calls:
        path = c["uri"].split("?")[0]
        out.append(f'{c["method"]} {path.split("/v1/")[-1].split("/v3/")[-1]}')
    return out


# ── incremental: Google Docs ─────────────────────────────────

def test_second_run_of_unchanged_ticket_is_skipped(env, services, llm, google_http, tmp_path):
    google_http.outcomes += google_create()
    assert main(["DEMO-2", "--publish", "gdocs"]) == 0
    assert llm.calls == 1 and len(google_http.calls) == 2

    assert main(["DEMO-2", "--publish", "gdocs"]) == 0
    assert llm.calls == 1, "no tokens spent on an unchanged ticket"
    assert len(google_http.calls) == 2, "no Google calls either"
    [doc] = store(tmp_path).list_documents()
    assert (doc.ticket_key, doc.target, doc.external_id, doc.version) == ("DEMO-2", "gdocs", "doc1", 1)


def test_changed_ticket_updates_the_same_doc_in_place(env, services, jira, llm, google_http, tmp_path,
                                                      capsys):
    google_http.outcomes += google_create()
    assert main(["DEMO-2", "--publish", "gdocs"]) == 0

    jira.add("DEMO-2", description="Use JWT with refresh tokens.")
    llm.text = DOC_V2
    google_http.outcomes += google_update()
    assert main(["DEMO-2", "--publish", "gdocs", "--show-diff"]) == 0

    calls = google_http.calls[2:]
    assert api_names(calls) == ["GET files/doc1", "GET documents/doc1", "POST documents/doc1:batchUpdate"]
    body = calls[2]["body"]
    assert body["writeControl"] == {"requiredRevisionId": "r7"}       # pinned to the revision just read
    requests = body["requests"]
    assert requests[0] == {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 59}}}
    assert "insertText" in requests[1]
    assert {"updateParagraphStyle", "deleteParagraphBullets", "updateTextStyle"} <= \
        {next(iter(r)) for r in requests[2:5]}

    [doc] = store(tmp_path).list_documents()
    assert doc.version == 2 and doc.external_id == "doc1" and "Second version" in doc.markdown

    out = capsys.readouterr()
    assert "added: Rollout" in out.out and "changed: Overview" in out.out
    assert "-First version of the overview." in out.out and "+Second version of the overview." in out.out
    assert "document changed" in out.err


def test_new_ticket_title_renames_the_doc(env, services, jira, llm, google_http):
    google_http.outcomes += google_create()
    main(["DEMO-2", "--publish", "gdocs"])
    jira.add("DEMO-2", summary="Add login v2")
    google_http.outcomes += [*google_update(title="Add login")[:2], (200, {"id": "doc1"}), (200, {})]
    assert main(["DEMO-2", "--publish", "gdocs"]) == 0
    rename = google_http.calls[4]
    assert rename["method"] == "PATCH" and rename["body"] == {"name": "Add login v2"}


def test_same_output_after_regeneration_skips_publish(env, services, jira, llm, google_http, tmp_path):
    google_http.outcomes += google_create()
    main(["DEMO-2", "--publish", "gdocs"])
    jira.add("DEMO-2", description="Use JWT. (typo fixed)")   # source changed...
    assert main(["DEMO-2", "--publish", "gdocs"]) == 0         # ...but the model wrote the same doc
    assert llm.calls == 2 and len(google_http.calls) == 2
    [doc] = store(tmp_path).list_documents()
    assert doc.version == 1
    # the new source hash is remembered, so the next run is skipped outright
    main(["DEMO-2", "--publish", "gdocs"])
    assert llm.calls == 2


def test_force_regenerates_and_republishes(env, services, llm, google_http):
    google_http.outcomes += google_create()
    main(["DEMO-2", "--publish", "gdocs"])
    google_http.outcomes += google_update()
    assert main(["DEMO-2", "--publish", "gdocs", "--force"]) == 0
    assert llm.calls == 2 and len(google_http.calls) == 5


@pytest.mark.parametrize("gone", ["trashed", "deleted"])
def test_missing_doc_is_recreated(env, services, jira, llm, google_http, tmp_path, gone):
    google_http.outcomes += google_create("doc1")
    main(["DEMO-2", "--publish", "gdocs"])
    jira.add("DEMO-2", description="changed")
    llm.text = DOC_V2
    lookup = ([(200, {"id": "doc1", "trashed": True})] if gone == "trashed"
              else [(404, {"error": {"code": 404, "message": "not found"}})])
    google_http.outcomes += [*lookup, *google_create("doc2")]
    assert main(["DEMO-2", "--publish", "gdocs"]) == 0
    [doc] = store(tmp_path).list_documents()
    assert doc.external_id == "doc2" and doc.version == 1


def test_no_store_creates_a_new_doc_every_time(env, services, google_http, tmp_path):
    google_http.outcomes += [*google_create("a"), *google_create("b")]
    assert main(["DEMO-2", "--publish", "gdocs", "--no-store"]) == 0
    assert main(["DEMO-2", "--publish", "gdocs", "--no-store"]) == 0
    assert len(google_http.calls) == 4
    assert not (tmp_path / ".docgen" / "docgen.db").exists()


def test_store_location_from_flag_and_env(env, services, monkeypatch, tmp_path):
    assert main(["DEMO-2", "--store", str(tmp_path / "custom.db")]) == 0
    assert Store(tmp_path / "custom.db").get_document("DEMO-2", "technical", "local")
    monkeypatch.setenv("DOCGEN_STORE", str(tmp_path / "env.db"))
    assert main(["DEMO-2"]) == 0
    assert Store(tmp_path / "env.db").get_document("DEMO-2", "technical", "local")


def test_local_only_runs_are_incremental_too(env, services, llm, tmp_path):
    assert main(["DEMO-2"]) == 0
    assert main(["DEMO-2"]) == 0
    assert llm.calls == 1
    doc = store(tmp_path).get_document("DEMO-2", "technical", "local")
    assert doc.url.endswith("DEMO-2_document.md")


def test_templates_are_tracked_separately(env, services, llm, tmp_path):
    main(["DEMO-2"])
    main(["DEMO-2", "--template", "runbook"])
    assert llm.calls == 2
    assert {d.template for d in store(tmp_path).list_documents()} == {"technical", "runbook"}


# ── incremental: Confluence ──────────────────────────────────

def test_confluence_page_is_updated_with_next_version(env, monkeypatch, jira, llm, tmp_path):
    pages = []

    def confluence(method, url, kwargs):
        pages.append((method, url, kwargs))
        if method == "POST":
            return make_response(200, {"id": "77", "_links": {"webui": "/spaces/KAN/pages/77"}})
        if method == "GET":
            return make_response(200, {"id": "77", "version": {"number": 3}})
        return make_response(200, {"id": "77", "_links": {"webui": "/spaces/KAN/pages/77"}})

    Router(monkeypatch, {CONFLUENCE: confluence, JIRA: jira, OPENROUTER: llm})
    assert main(["DEMO-2", "--publish", "confluence"]) == 0
    jira.add("DEMO-2", description="changed")
    llm.text = DOC_V2
    assert main(["DEMO-2", "--publish", "confluence"]) == 0
    method, url, kwargs = pages[-1]
    assert method == "PUT" and url.endswith("/content/77")
    assert kwargs["json"]["version"]["number"] == 4
    assert store(tmp_path).get_document("DEMO-2", "technical", "confluence").version == 2


def test_deleted_confluence_page_is_recreated(env, monkeypatch, jira, llm, tmp_path):
    calls = []

    def confluence(method, url, kwargs):
        calls.append(method)
        if method == "GET":
            return make_response(404, text="gone")
        return make_response(200, {"id": str(len(calls)), "_links": {"webui": f"/p/{len(calls)}"}})

    Router(monkeypatch, {CONFLUENCE: confluence, JIRA: jira, OPENROUTER: llm})
    main(["DEMO-2", "--publish", "confluence"])
    jira.add("DEMO-2", description="changed")
    llm.text = DOC_V2
    assert main(["DEMO-2", "--publish", "confluence"]) == 0
    assert calls == ["POST", "GET", "POST"]


# ── docs command ─────────────────────────────────────────────

def test_docs_command_lists_the_store(env, services, capsys):
    assert main(["docs"]) == 0
    assert "No documents recorded yet." in capsys.readouterr().out
    main(["DEMO-2"])
    capsys.readouterr()
    assert main(["docs"]) == 0
    out = capsys.readouterr().out
    assert "DEMO-2" in out and "local" in out and "v1" in out
    assert main(["docs", "demo-2", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert row["ticket_key"] == "DEMO-2" and "markdown" not in row


def test_docs_command_errors(capsys):
    assert main(["docs", "--no-store"]) == 2
    assert main(["docs", "bad key"]) == 2


# ── batch: JQL + concurrency ─────────────────────────────────

def test_jql_batch_runs_concurrently(env, services, jira, llm, tmp_path, capsys):
    for n in range(3, 9):
        jira.add(f"DEMO-{n}", summary=f"Ticket {n}")
    seen_threads = set()
    original = llm.__call__

    def tracking(method, url, kwargs):
        seen_threads.add(threading.current_thread().name)
        return original(method, url, kwargs)

    services.routes[OPENROUTER] = tracking
    assert main(["--jql", "project = DEMO", "-j", "4"]) == 0
    assert llm.calls == 7
    assert len(seen_threads) > 1 and all(t.startswith("docgen") for t in seen_threads)
    assert len(list(tmp_path.glob("DEMO-*_document.md"))) == 7
    err = capsys.readouterr().err
    assert "progress  done=7 total=7" in err  # non-TTY: progress is logged
    assert "succeeded=7" in err


def test_jql_and_explicit_tickets_are_merged(env, services, jira, llm):
    jira.add("DEMO-3")
    assert main(["DEMO-2", "--jql", "key = DEMO-3"]) == 0
    assert llm.calls == 2  # DEMO-2 appears in both, processed once


def test_jql_with_no_matches(env, monkeypatch, capsys):
    Router(monkeypatch, {"search/jql": [make_response(200, {"issues": [], "isLast": True})]})
    assert main(["--jql", "project = NOPE"]) == 0
    assert "no issues match" in capsys.readouterr().err


def test_invalid_jql_is_exit_2(env, monkeypatch, capsys):
    Router(monkeypatch, {"search/jql": [make_response(400, {"errorMessages": ["bad field"]})]})
    assert main(["--jql", "projekt = DEMO"]) == 2
    assert "bad field" in capsys.readouterr().err


def test_auth_failure_cancels_the_batch_and_leaves_no_threads(env, services, jira):
    for n in range(3, 12):
        jira.add(f"DEMO-{n}")
    jira.fail["DEMO-5"] = make_response(401, text="no")
    assert main(["--jql", "project = DEMO", "-j", "3"]) == 2
    assert not [t for t in threading.enumerate() if t.name.startswith("docgen")]


def test_one_failure_does_not_stop_a_concurrent_batch(env, services, jira, llm, tmp_path):
    for n in range(3, 7):
        jira.add(f"DEMO-{n}")
    jira.fail["DEMO-4"] = make_response(404, text="gone")
    assert main(["--jql", "project = DEMO", "-j", "4"]) == 1
    assert len(list(tmp_path.glob("DEMO-*_document.md"))) == 4


@pytest.mark.parametrize("flag", [["-j", "0"], ["--max-results", "0"]])
def test_bad_batch_numbers(env, flag):
    assert main(["DEMO-2", *flag]) == 2


# ── images end to end ────────────────────────────────────────

ATTACHMENT = [{"id": 10, "filename": "flow diagram.png", "mimeType": "image/png", "size": len(PNG),
               "content": "https://example.atlassian.net/rest/api/3/attachment/content/10"}]


def test_full_context_embeds_images_in_google_docs(env, services, jira, llm, google_http, tmp_path):
    jira.add("DEMO-2", attachment=ATTACHMENT)
    llm.text = "## Overview\n\nThe flow: ![Login flow](attachment:flow_diagram.png) as shown.\n"
    google_http.outcomes += [
        *google_create(),
        (200, {"id": "tmp1", "webContentLink": "https://drive.google.com/uc?id=tmp1&export=download"}),
        (200, {"id": "perm"}),            # temporary link share
        (200, {"replies": []}),           # insertInlineImage
        (204, {}),                        # delete the temporary Drive file
    ]
    assert main(["DEMO-2", "--publish", "gdocs", "--context", "full"]) == 0

    prompt = services.calls_to(OPENROUTER)[0][2]["json"]["messages"][1]["content"]
    assert "flow_diagram.png" in prompt and "attachment:FILENAME" in prompt

    upload, share, insert, delete = google_http.calls[2:]
    assert "upload" in upload["uri"]
    assert share["body"] == {"type": "anyone", "role": "reader"}
    image = insert["body"]["requests"][0]["insertInlineImage"]
    assert image["uri"].startswith("https://drive.google.com/uc?id=tmp1")
    assert image["objectSize"]["width"]["unit"] == "PT"
    assert delete["method"] == "DELETE" and delete["uri"].split("?")[0].endswith("/files/tmp1")

    saved = (tmp_path / "DEMO-2_document.md").read_text()
    assert "![Login flow](DEMO-2_attachments/flow_diagram.png)" in saved
    assert (tmp_path / "DEMO-2_attachments" / "flow_diagram.png").read_bytes() == PNG


def test_unplaced_images_are_appended_and_failures_leave_placeholders(env, services, jira, llm,
                                                                      google_http, tmp_path):
    jira.add("DEMO-2", attachment=ATTACHMENT)
    llm.text = "## Overview\n\nNo image here, and ![ghost](attachment:invented.png) is dropped.\n"
    google_http.outcomes += [
        *google_create(),
        (200, {"id": "tmp1"}),                                     # upload ok (no webContentLink)
        (200, {"id": "perm"}),
        (400, {"error": {"code": 400, "message": "Problem retrieving image"}}),
        (200, {"replies": []}),                                    # placeholder text instead
        (500, {"error": {"code": 500, "message": "x"}}), *[(500, {"error": {"code": 500}})] * 3,
    ]
    assert main(["DEMO-2", "--publish", "gdocs", "--context", "full"]) == 0
    saved = (tmp_path / "DEMO-2_document.md").read_text()
    assert "invented.png" not in saved and "ghost" in saved
    assert "## Attachments" in saved and "flow_diagram.png" in saved
    placeholder = google_http.calls[5]["body"]["requests"][0]["insertText"]
    assert placeholder["text"] == "[image: flow_diagram.png]"


def test_confluence_uploads_images_as_attachments(env, monkeypatch, jira, llm, tmp_path):
    jira.add("DEMO-2", attachment=ATTACHMENT)
    llm.text = "## Overview\n\n![Login flow](attachment:flow_diagram.png)\n"
    pages = []

    def confluence(method, url, kwargs):
        pages.append((method, url, kwargs))
        return make_response(200, {"id": "5", "_links": {"webui": "/p/5"}})

    Router(monkeypatch, {CONFLUENCE: confluence, JIRA: jira, OPENROUTER: llm})
    assert main(["DEMO-2", "--publish", "confluence", "--context", "full"]) == 0
    create, upload = pages
    assert '<ri:attachment ri:filename="flow_diagram.png" />' in create[2]["json"]["body"]["storage"]["value"]
    assert upload[0] == "PUT" and upload[1].endswith("/content/5/child/attachment")
    assert upload[2]["headers"]["X-Atlassian-Token"] == "no-check"
    assert upload[2]["files"]["file"][0] == "flow_diagram.png"


def test_confluence_image_upload_failure_is_not_fatal(env, monkeypatch, jira, llm, sleeps):
    jira.add("DEMO-2", attachment=ATTACHMENT)
    llm.text = "## Overview\n\ntext\n"

    def confluence(method, url, kwargs):
        if "child/attachment" in url:
            return make_response(413, text="too big")
        return make_response(200, {"id": "5", "_links": {"webui": "/p/5"}})

    Router(monkeypatch, {CONFLUENCE: confluence, JIRA: jira, OPENROUTER: llm})
    assert main(["DEMO-2", "--publish", "confluence", "--context", "full"]) == 0
