"""Richer ticket context: comments, links, subtasks, attachments, JQL search."""
import json

import pytest
import requests

from conftest import FakeTransport, Router, make_response
from docgen.errors import APIError, InvalidJQLError, TicketNotFoundError
from docgen.eval.scorers import faithfulness
from docgen.jira import JiraClient
from docgen.llm import build_messages
from docgen.models import Attachment, Comment, ContextLevel, ContextOptions, RelatedIssue, Ticket
from docgen.templates import load_template

JIRA = "atlassian.net"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


def adf(text):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def rich_issue(key="DEMO-2"):
    return {
        "key": key,
        "fields": {
            "summary": "Add login", "description": adf("Use JWT."),
            "status": {"name": "In Progress"}, "labels": ["auth"],
            "issuetype": {"name": "Story"}, "priority": {"name": "High"},
            "comment": {"comments": [
                {"author": {"displayName": f"Dev {n}"}, "created": f"2026-01-0{n}T10:00:00.000+0000",
                 "body": adf(f"comment {n}")} for n in range(1, 4)
            ] + [{"author": {"displayName": "Empty"}, "body": adf("")}]},
            "issuelinks": [
                {"type": {"inward": "is blocked by", "outward": "blocks"},
                 "outwardIssue": {"key": "DEMO-9", "fields": {"summary": "Rate limiter",
                                                              "status": {"name": "Done"}}}},
                {"type": {"inward": "is blocked by", "outward": "blocks"},
                 "inwardIssue": {"key": "DEMO-1", "fields": {"summary": "User table"}}},
                {"type": {"name": "broken"}},
            ],
            "subtasks": [{"key": "DEMO-3", "fields": {"summary": "Write tests",
                                                      "status": {"name": "To Do"}}}],
            "attachment": [
                {"id": 10, "filename": "flow diagram.png", "mimeType": "image/png", "size": len(PNG),
                 "content": "https://example.atlassian.net/rest/api/3/attachment/content/10"},
                {"id": 11, "filename": "spec.pdf", "mimeType": "application/pdf", "size": 5000,
                 "content": "https://example.atlassian.net/rest/api/3/attachment/content/11"},
            ],
        },
    }


def binary(data, status=200):
    r = make_response(status, text="")
    r._content = data
    r._content_consumed = True  # iter_content then yields from _content
    return r


# ── context levels ───────────────────────────────────────────

def test_minimal_context_asks_only_for_base_fields(monkeypatch, jira_env):
    transport = FakeTransport(monkeypatch, make_response(200, rich_issue()))
    ticket = JiraClient().get_ticket("DEMO-2", ContextLevel.MINIMAL)
    fields = transport.calls[0][2]["params"]["fields"].split(",")
    assert "comment" not in fields and "issuelinks" not in fields
    assert ticket.comments == [] and ticket.links == [] and ticket.attachments == []
    assert ticket.issue_type == "Story" and ticket.priority == "High"


def test_standard_context_parses_comments_links_subtasks(monkeypatch, jira_env):
    transport = FakeTransport(monkeypatch, make_response(200, rich_issue()))
    ticket = JiraClient().get_ticket("DEMO-2", ContextLevel.STANDARD)
    fields = transport.calls[0][2]["params"]["fields"]
    assert all(f in fields for f in ("comment", "issuelinks", "subtasks", "attachment"))
    assert [c.body for c in ticket.comments] == ["comment 1", "comment 2", "comment 3"]  # empty dropped
    assert ticket.comments[0].author == "Dev 1"
    assert [(link.key, link.relation, link.status) for link in ticket.links] == \
        [("DEMO-9", "blocks", "Done"), ("DEMO-1", "is blocked by", "")]
    assert ticket.subtasks == [RelatedIssue(key="DEMO-3", summary="Write tests", status="To Do")]
    assert [a.filename for a in ticket.attachments] == ["flow diagram.png", "spec.pdf"]
    assert ticket.images == []  # names only: nothing downloaded at standard
    assert len(transport.calls) == 1


def test_comment_limit_keeps_the_newest(monkeypatch, jira_env):
    FakeTransport(monkeypatch, make_response(200, rich_issue()))
    options = ContextOptions.for_level("standard").model_copy(update={"max_comments": 2})
    ticket = JiraClient().get_ticket("DEMO-2", options)
    assert [c.body for c in ticket.comments] == ["comment 3"]  # last 2 raw = comment 3 + empty


def test_full_context_fetches_related_descriptions_and_images(monkeypatch, jira_env, tmp_path):
    def jira(method, url, kwargs):
        if url.endswith("/attachment/content/10"):
            return binary(PNG)
        key = url.rsplit("/", 1)[-1]
        if key == "DEMO-2":
            return make_response(200, rich_issue())
        if key == "DEMO-1":
            return make_response(404, text="gone")       # an issue we can't see
        return make_response(200, {"key": key, "fields": {"description": adf(f"about {key}")}})

    router = Router(monkeypatch, {JIRA: jira})
    ticket = JiraClient(cache_dir=tmp_path / "cache").get_ticket("DEMO-2", ContextLevel.FULL)

    assert ticket.subtasks[0].description == "about DEMO-3"
    assert ticket.links[0].description == "about DEMO-9"
    assert ticket.links[1].description is None            # 404 skipped, not fatal
    [image] = ticket.images
    assert image.filename == "flow diagram.png"
    assert (tmp_path / "cache" / "DEMO-2" / "10-flow_diagram.png").read_bytes() == PNG
    assert not router.calls_to("content/11")              # PDFs are never downloaded

    # second fetch reuses the cached file
    before = len(router.calls_to("content/10"))
    JiraClient(cache_dir=tmp_path / "cache").get_ticket("DEMO-2", ContextLevel.FULL)
    assert len(router.calls_to("content/10")) == before


def test_oversized_and_failed_images_are_skipped(monkeypatch, jira_env, tmp_path):
    issue = rich_issue()
    issue["fields"]["attachment"].append({"id": 12, "filename": "huge.png", "mimeType": "image/png",
                                          "size": 50 * 1024 * 1024, "content": "https://x/12"})
    Router(monkeypatch, {"content/10": [make_response(500, text="boom")] * 4,
                         JIRA: lambda m, u, k: make_response(200, issue if u.endswith("DEMO-2") else
                                                             {"key": "X-1", "fields": {}})})
    ticket = JiraClient(cache_dir=tmp_path).get_ticket("DEMO-2", ContextLevel.FULL)
    assert ticket.images == []


def test_download_larger_than_declared_is_rejected(monkeypatch, jira_env, tmp_path):
    """Jira says 10 bytes; the stream keeps going past the 50-byte limit and is cut off."""
    issue = rich_issue()
    issue["fields"]["attachment"][0]["size"] = 10
    options = ContextOptions.for_level("full").model_copy(update={"max_image_bytes": 50,
                                                                  "related_descriptions": False})
    Router(monkeypatch, {"content/10": [binary(PNG)], JIRA: lambda m, u, k: make_response(200, issue)})
    ticket = JiraClient(cache_dir=tmp_path).get_ticket("DEMO-2", options)
    assert ticket.images == []
    assert not list(tmp_path.rglob("*.png"))


def test_interrupted_download_is_transient(monkeypatch, jira_env, tmp_path, sleeps):
    class Broken:
        status_code, ok, headers, text = 200, True, {}, ""

        def iter_content(self, size):
            raise requests.ConnectionError("reset")

        def close(self):
            pass

    calls = {"n": 0}

    def attachment(m, u, k):
        calls["n"] += 1
        return Broken() if calls["n"] == 1 else binary(PNG)

    Router(monkeypatch, {"content/10": attachment,
                         JIRA: lambda m, u, k: make_response(200, rich_issue() if u.endswith("DEMO-2") else
                                                             {"key": "X-1", "fields": {}})})
    ticket = JiraClient(cache_dir=tmp_path).get_ticket("DEMO-2", ContextLevel.FULL)
    assert len(ticket.images) == 1 and len(sleeps) >= 1


def test_too_many_images_are_capped(monkeypatch, jira_env, tmp_path):
    issue = rich_issue()
    issue["fields"]["attachment"] = [
        {"id": n, "filename": f"i{n}.png", "mimeType": "image/png", "size": len(PNG),
         "content": f"https://example.atlassian.net/rest/api/3/attachment/content/{n}"} for n in range(5)]
    options = ContextOptions.for_level("full").model_copy(update={"max_images": 2,
                                                                  "related_descriptions": False})
    Router(monkeypatch, {"attachment/content": lambda m, u, k: binary(PNG),
                         JIRA: lambda m, u, k: make_response(200, issue)})
    assert len(JiraClient(cache_dir=tmp_path).get_ticket("DEMO-2", options).images) == 2


def test_non_object_issue_response(monkeypatch, jira_env):
    FakeTransport(monkeypatch, make_response(200, text="[]"))
    with pytest.raises(APIError, match="not an object"):
        JiraClient().get_ticket("DEMO-2")


# ── prompt and scoring use the context ───────────────────────

def ticket_with_context(**extra):
    return Ticket(key="DEMO-2", title="Add login", description="Use JWT.", issue_type="Story",
                  priority="High",
                  comments=[Comment(author="PM", created="2026-01-02T10:00",
                                    body="Tokens expire after 15 minutes")],
                  links=[RelatedIssue(key="DEMO-9", relation="blocks", summary="Rate limiter",
                                      status="Done", description="Limit to 5 attempts\nper minute")],
                  subtasks=[RelatedIssue(key="DEMO-3", summary="Write tests")],
                  **extra)


def test_context_block_in_prompt():
    user = build_messages(ticket_with_context(), load_template("technical"))[1]["content"]
    assert "CONTEXT (comments, related issues and attachments" in user
    assert "ISSUE TYPE: Story · PRIORITY: High" in user
    assert "- [2026-01-02] PM: Tokens expire after 15 minutes" in user
    assert "- blocks DEMO-9: Rate limiter (Done)" in user
    assert "  Limit to 5 attempts\n  per minute" in user
    assert "- DEMO-3: Write tests" in user


def test_minimal_ticket_has_no_context_block():
    user = build_messages(Ticket(key="DEMO-2", title="t"), load_template("technical"))[1]["content"]
    assert "CONTEXT" not in user


def test_images_are_offered_to_the_model(tmp_path):
    image = Attachment(id="1", filename="flow.png", mime_type="image/png", local_path=str(tmp_path / "f"))
    pdf = Attachment(id="2", filename="spec.pdf", mime_type="application/pdf")
    ticket = ticket_with_context(attachments=[image, pdf])
    user = build_messages(ticket, load_template("technical"))[1]["content"]
    assert "![short caption](attachment:FILENAME)" in user and "flow.png" in user
    assert "- flow.png (image, can be embedded)" in user and "- spec.pdf\n" in user + "\n"


def test_context_is_trimmed_oldest_comments_first():
    comments = [Comment(author="a", created=f"2026-01-{n:02d}", body=f"note {n} " + "x" * 200)
                for n in range(1, 31)]
    ticket = Ticket(key="DEMO-2", title="t", comments=comments,
                    subtasks=[RelatedIssue(key="DEMO-3", summary="kept")])
    text = ticket.context_text(max_chars=2000)
    assert len(text) <= 2000
    assert "note 30" in text and "note 1 " not in text
    assert "older omitted" in text and "DEMO-3: kept" in text


def test_context_hard_cap_when_nothing_left_to_drop():
    ticket = Ticket(key="DEMO-2", title="t",
                    subtasks=[RelatedIssue(key="DEMO-3", summary="s" * 500)])
    assert ticket.context_text(max_chars=100).endswith("…[truncated]")


def test_faithfulness_counts_comments_as_source():
    doc = "## Technical Details\n\nTokens expire after 15 minutes; logins are limited to 5 attempts."
    ticket = ticket_with_context()
    assert faithfulness(doc, ticket, load_template("technical")).value == 1.0
    bare = Ticket(key="DEMO-2", title="Add login", description="Use JWT.")
    assert faithfulness(doc, bare, load_template("technical")).value < 1.0


def test_fingerprint_tracks_content_not_cache_paths(tmp_path):
    a = ticket_with_context(attachments=[Attachment(id="1", filename="f.png", mime_type="image/png")])
    b = a.model_copy(deep=True)
    b.attachments[0].local_path = str(tmp_path / "f.png")
    assert a.fingerprint() == b.fingerprint()
    c = a.model_copy(update={"comments": [*a.comments, Comment(body="new")]})
    assert a.fingerprint() != c.fingerprint()


# ── JQL search ───────────────────────────────────────────────

def test_search_paginates_with_next_page_token(monkeypatch, jira_env):
    router = Router(monkeypatch, {"search/jql": [
        make_response(200, {"issues": [{"key": "DEMO-1"}, {"key": "DEMO-2"}], "nextPageToken": "p2",
                            "isLast": False}),
        make_response(200, {"issues": [{"key": "DEMO-2"}, {"key": "DEMO-3"}], "isLast": True}),
    ]})
    assert JiraClient().search("project = DEMO") == ["DEMO-1", "DEMO-2", "DEMO-3"]
    first, second = (c[2]["json"] for c in router.calls)
    assert first == {"jql": "project = DEMO", "fields": ["summary"], "maxResults": 100}
    assert second["nextPageToken"] == "p2"


def test_search_respects_max_results(monkeypatch, jira_env):
    router = Router(monkeypatch, {"search/jql": [
        make_response(200, {"issues": [{"key": f"DEMO-{n}"} for n in range(1, 4)],
                            "nextPageToken": "more", "isLast": False})]})
    assert JiraClient().search("project = DEMO", max_results=3) == ["DEMO-1", "DEMO-2", "DEMO-3"]
    assert router.calls[0][2]["json"]["maxResults"] == 3 and len(router.calls) == 1


def test_invalid_jql_is_explained(monkeypatch, jira_env):
    FakeTransport(monkeypatch, make_response(400, {"errorMessages": ["Field 'projekt' does not exist"]}))
    with pytest.raises(InvalidJQLError, match="projekt"):
        JiraClient().search("projekt = DEMO")


def test_invalid_jql_without_json_body(monkeypatch, jira_env):
    FakeTransport(monkeypatch, make_response(400, text="Bad Request"))
    with pytest.raises(InvalidJQLError, match="Bad Request"):
        JiraClient().search("x")


@pytest.mark.parametrize("bad", [make_response(200, text="nope"), make_response(200, {"total": 1})])
def test_malformed_search_response(monkeypatch, jira_env, bad):
    FakeTransport(monkeypatch, bad)
    with pytest.raises(APIError, match="unexpected Jira search response"):
        JiraClient().search("project = DEMO")


def test_search_auth_errors_reuse_issue_messages(monkeypatch, jira_env):
    FakeTransport(monkeypatch, make_response(404, text="x"))
    with pytest.raises(TicketNotFoundError):
        JiraClient().search("project = DEMO")


def test_sessions_are_per_thread(jira_env):
    import threading
    client = JiraClient()
    seen = []
    t = threading.Thread(target=lambda: seen.append(client.session))
    t.start()
    t.join()
    assert seen[0] is not client.session and client.session is client.session


def test_rich_issue_json_roundtrip():
    # guard against the fixture drifting from what the parser expects
    assert json.loads(json.dumps(rich_issue()))["fields"]["issuelinks"][0]["outwardIssue"]["key"] == "DEMO-9"
