"""Unit tests for the store, change summaries, progress display and image resolution."""
import io
import logging
import sqlite3

import pytest

from docgen.assets import image_name, localize, resolve_images
from docgen.diff import colorize, summarize_changes
from docgen.logging_setup import ProgressAwareHandler, TextFormatter
from docgen.models import Attachment, ChangeSummary, Ticket
from docgen.progress import Progress, active
from docgen.store import Store, StoredDocument

# ── store ────────────────────────────────────────────────────


def record(**kw):
    base = dict(ticket_key="DEMO-2", template="technical", target="gdocs", external_id="d1",
                url="https://x/d1", title="T", source_hash="s1", content_hash="c1", markdown="# T")
    return StoredDocument(**{**base, **kw})


def test_document_upsert_keeps_created_at(tmp_path):
    store = Store(tmp_path / "nested" / "db.sqlite")
    first = store.save_document(record())
    store.save_document(record(version=2, markdown="# T v2", created_at=first.created_at))
    doc = store.get_document("DEMO-2", "technical", "gdocs")
    assert doc.version == 2 and doc.markdown == "# T v2" and doc.created_at == first.created_at
    assert store.get_document("DEMO-2", "technical", "confluence") is None
    store.save_document(record(ticket_key="DEMO-3"))
    assert [d.ticket_key for d in store.list_documents("DEMO-3")] == ["DEMO-3"]
    assert len(store.list_documents()) == 2


def test_schema_version_and_wal(tmp_path):
    Store(tmp_path / "db.sqlite")
    db = sqlite3.connect(tmp_path / "db.sqlite")
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_jobs_lifecycle_and_dedupe(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    job, created = store.create_job("webhook", {"tickets": ["DEMO-2"]}, dedupe_key="k1")
    again, created_again = store.create_job("webhook", {"tickets": ["DEMO-2"]}, dedupe_key="k1")
    assert created and not created_again and again.id == job.id
    store.update_job(job.id, status="running")
    store.update_job(job.id, status="succeeded", results=[{"ticket": "DEMO-2"}])
    store.update_job(job.id, status="succeeded")  # results kept when not given
    done = store.get_job(job.id)
    assert done.status == "succeeded" and done.results == [{"ticket": "DEMO-2"}]
    assert store.get_job("missing") is None and store.find_job("nope") is None

    other, _ = store.create_job("generate", {})
    assert [j.id for j in store.list_jobs()] == [other.id, job.id]
    assert store.mark_interrupted() == 1
    assert store.get_job(other.id).status == "interrupted"


def test_store_is_safe_across_threads(tmp_path):
    import threading
    store = Store(tmp_path / "db.sqlite")

    def write(n):
        for i in range(20):
            store.save_document(record(ticket_key=f"DEMO-{n}", version=i + 1))

    threads = [threading.Thread(target=write, args=(n,)) for n in range(2, 8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {d.version for d in store.list_documents()} == {20}


# ── change summaries ─────────────────────────────────────────

OLD = "# Title\n\n## A\n\none\n\n## B\n\ntwo\n"
NEW = "# Title\n\n## A\n\none, edited\n\n## C\n\nthree\n"


def test_summary_names_added_removed_and_changed_sections():
    s = summarize_changes(OLD, NEW, previous_version=3)
    assert (s.added_sections, s.removed_sections, s.changed_sections) == (["C"], ["B"], ["A"])
    assert s.previous_version == 3 and s.diff.startswith("--- v3\n+++ v4")
    assert s.describe() == "+3/-3 lines; added: C; removed: B; changed: A"


def test_identical_documents_have_no_changes():
    s = summarize_changes(OLD, OLD)
    assert not s.changed and s.describe() == "no changes" and s.diff == ""


def test_documents_without_headings_and_huge_diffs():
    assert summarize_changes("plain", "other").changed_sections == []
    big = summarize_changes("a\n" * 10, "b\n" * 30000)
    assert big.diff.endswith("… diff truncated") and len(big.diff) < 21000


def test_colorize_marks_additions_and_removals():
    colored = colorize("--- v1\n+++ v2\n@@ -1 +1 @@\n-old\n+new\n same")
    assert "\033[31m-old" in colored and "\033[32m+new" in colored and "\033[36m@@" in colored
    assert "--- v1\n+++ v2" in colored and colored.endswith(" same")


def test_change_summary_describe_partial():
    assert ChangeSummary(previous_version=1, lines_added=2).describe() == "+2/-0 lines"


# ── progress ─────────────────────────────────────────────────

class Tty(io.StringIO):
    def isatty(self):
        return True


def test_progress_bar_draws_and_clears_on_a_terminal():
    stream = Tty()
    with Progress(4, stream=stream) as bar:
        assert active() is bar
        bar.start("DEMO-1")
        bar.finish("DEMO-1", ok=True, action="created")
        bar.finish("DEMO-2", ok=False)
        bar.finish("DEMO-3", ok=True, action="unchanged")
        line = bar.line()
    assert active() is None
    assert "3/4" in line and "✓2 ✗1" in line and "=1 unchanged" in line and "ETA" in line
    assert "▕███████████████░░░░░▏" in line
    assert stream.getvalue().endswith("\r\033[2K")  # cleared on exit


def test_progress_lists_running_tickets():
    bar = Progress(10, enabled=False)
    for n in range(5):
        bar.start(f"DEMO-{n}")
    assert "running DEMO-0, DEMO-1, DEMO-2 +2" in bar.line()


def test_progress_off_a_terminal_logs_instead(caplog):
    stream = io.StringIO()
    with caplog.at_level(logging.INFO, logger="docgen.progress"), Progress(2, stream=stream) as bar:
        bar.finish("DEMO-1", ok=True, action="saved")
    assert stream.getvalue() == ""
    assert caplog.records[-1].done == 1 and caplog.records[-1].status == "saved"


def test_progress_with_zero_total_is_full():
    assert "▕████████████████████▏ 0/0" in Progress(0, enabled=False).line()


def test_log_lines_clear_and_redraw_the_bar():
    stream = Tty()
    handler = ProgressAwareHandler(stream)
    handler.setFormatter(TextFormatter())
    record_ = logging.LogRecord("docgen.x", logging.INFO, __file__, 1, "hello", None, None)
    with Progress(2, stream=stream):
        handler.emit(record_)
    out = stream.getvalue()
    hello = out.index("hello")
    assert out.rfind("\r\033[2K", 0, hello) != -1           # bar cleared before the record
    assert "0/2" in out[hello:]                              # and redrawn after it
    handler.emit(record_)                                    # no bar: plain write
    assert stream.getvalue().endswith("hello\n")


# ── image resolution ─────────────────────────────────────────

def image(tmp_path, filename="flow diagram.png", n="1"):
    path = tmp_path / f"cache-{n}.png"
    path.write_bytes(b"png")
    return Attachment(id=n, filename=filename, mime_type="image/png", size=3, local_path=str(path))


def test_image_names_are_link_safe():
    assert image_name(Attachment(id="1", filename="dir/My Flow (v2).PNG")) == "My_Flow_v2_.PNG"
    assert image_name(Attachment(id="9", filename="...")) == "image-9"


def test_resolve_moves_inline_refs_to_own_line_and_dedupes(tmp_path):
    ticket = Ticket(key="DEMO-2", title="t", attachments=[image(tmp_path)])
    text = "See ![a](attachment:flow_diagram.png) and ![b](attachment:flow_diagram.png)."
    body, images = resolve_images(text, ticket)
    assert body == ("See\n\n![a](attachment:flow_diagram.png)\n\nand\n\n"
                    "![b](attachment:flow_diagram.png)\n\n.\n")
    assert [i.id for i in images] == ["1"]


def test_resolve_drops_unknown_and_appends_unplaced(tmp_path, caplog):
    ticket = Ticket(key="DEMO-2", title="t", attachments=[image(tmp_path)])
    body, images = resolve_images("## A\n\n![made up](attachment:nope.png)\n", ticket, heading_level=3)
    assert "nope.png" not in body and "made up" in body
    assert "### Attachments\n\n![flow_diagram.png](attachment:flow_diagram.png)" in body
    assert "dropping reference to unknown image" in caplog.text
    assert len(images) == 1


def test_resolve_without_images_leaves_body_alone():
    body, images = resolve_images("## A\n\ntext", Ticket(key="DEMO-2", title="t"))
    assert body == "## A\n\ntext\n" and images == []


def test_localize_copies_and_rewrites(tmp_path):
    out = tmp_path / "out"
    imgs = [image(tmp_path), Attachment(id="2", filename="lost.png", mime_type="image/png")]
    body = "![x](attachment:flow_diagram.png)\n![y](attachment:lost.png)"
    result = localize(body, imgs, out / "DEMO-2_attachments", relative_to=out)
    assert result == "![x](DEMO-2_attachments/flow_diagram.png)\n![y](lost.png)"
    assert (out / "DEMO-2_attachments" / "flow_diagram.png").read_bytes() == b"png"
    assert localize("same", [], out / "a", out) == "same"


@pytest.mark.parametrize("markdown, slots", [
    ("![c](attachment:a.png)", [(1, "a.png", "c")]),
    ("x\n- item\n![c](attachment:a.png)\n- next", [(8, "a.png", "c")]),   # "x\n" + "item\n" = 7
])
def test_docs_image_slots(markdown, slots):
    from docgen.publishers.markdown_convert import markdown_to_docs_with_images
    plain, requests, found = markdown_to_docs_with_images(markdown)
    assert [tuple(s) for s in found] == slots
    assert plain[found[0].position - 1] == "\n"   # an empty paragraph waits for the image
    bullets = [r for r in requests if "createParagraphBullets" in r]
    assert len(bullets) == (2 if "item" in markdown else 0)  # the image splits the list
