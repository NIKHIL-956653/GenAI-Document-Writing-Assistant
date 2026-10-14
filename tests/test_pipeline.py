"""
Unit tests for the pure logic — no network, no API keys, no Jira.

These cover the three places where bugs actually bit: ADF parsing (Jira v3
returns a JSON tree, not a string), the prompt contract shared by both LLM
backends, and the markdown conversion that stops published docs showing
literal "## " characters.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jira_fetch import adf_to_text                      # noqa: E402
from llm_generate import build_messages                 # noqa: E402
from markdown_convert import (                          # noqa: E402
    markdown_to_docs,
    markdown_to_storage,
)


# ── adf_to_text ──────────────────────────────────────────────

def test_adf_none_and_plain_string():
    assert adf_to_text(None) == ""
    assert adf_to_text("already text") == "already text"


def test_adf_paragraph_extracts_prose():
    node = {
        "type": "doc",
        "content": [
            {"type": "paragraph",
             "content": [{"type": "text", "text": "Add JWT login."}]}
        ],
    }
    assert adf_to_text(node).strip() == "Add JWT login."


def test_adf_bullet_list_becomes_dashes():
    node = {
        "type": "bulletList",
        "content": [
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "First"}]}]},
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Second"}]}]},
        ],
    }
    text = adf_to_text(node)
    assert "- First" in text
    assert "- Second" in text


def test_adf_code_block_is_fenced():
    node = {
        "type": "codeBlock",
        "content": [{"type": "text", "text": "SELECT 1;"}],
    }
    assert adf_to_text(node) == "```\nSELECT 1;\n```\n"


def test_adf_hard_break_is_newline():
    node = {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "line one"},
            {"type": "hardBreak"},
            {"type": "text", "text": "line two"},
        ],
    }
    assert adf_to_text(node) == "line one\nline two\n"


def test_adf_unknown_node_still_returns_children():
    """Atlassian adds node types over time — unknown wrappers must not drop text."""
    node = {
        "type": "someFutureNodeType",
        "content": [{"type": "text", "text": "kept"}],
    }
    assert adf_to_text(node) == "kept"


# ── build_messages ───────────────────────────────────────────

@pytest.fixture
def ticket():
    return {
        "id": "DEMO-1",
        "title": "Build secure login system with JWT",
        "description": "Users need to authenticate with email and password.",
        "status": "To Do",
        "labels": ["auth", "backend"],
    }


def test_build_messages_shape(ticket):
    messages = build_messages(ticket)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


def test_build_messages_includes_every_ticket_field(ticket):
    user_content = build_messages(ticket)[1]["content"]
    assert "DEMO-1" in user_content
    assert "Build secure login system with JWT" in user_content
    assert "email and password" in user_content
    assert "To Do" in user_content
    assert "auth, backend" in user_content


def test_build_messages_handles_missing_labels(ticket):
    ticket["labels"] = None
    assert "none" in build_messages(ticket)[1]["content"]


def test_system_prompt_constrains_hallucination(ticket):
    system = build_messages(ticket)[0]["content"]
    assert "Known Limitations" in system


# ── markdown_to_docs ─────────────────────────────────────────

def test_docs_empty_input_returns_nothing():
    assert markdown_to_docs("") == ("", [])


def test_docs_first_request_is_the_text_insert():
    _, requests = markdown_to_docs("# Title\n\nSome prose.")
    assert "insertText" in requests[0]
    assert requests[0]["insertText"]["location"]["index"] == 1


def test_docs_strips_markdown_markers_from_text():
    """The whole point: the reader must not see '##' or '-' or '**'."""
    plain, _ = markdown_to_docs("## Overview\n\n- item one\n\n**bold** text")
    assert "##" not in plain
    assert "**" not in plain
    assert "- item one" not in plain
    assert "Overview" in plain
    assert "item one" in plain
    assert "bold" in plain


def test_docs_heading_levels_map_to_named_styles():
    _, requests = markdown_to_docs("# One\n## Two\n### Three")
    styles = [r["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"]
              for r in requests if "updateParagraphStyle" in r]
    assert styles == ["HEADING_1", "HEADING_2", "HEADING_3"]


def test_docs_consecutive_bullets_become_one_list():
    _, requests = markdown_to_docs("- a\n- b\n- c")
    bullets = [r for r in requests if "createParagraphBullets" in r]
    assert len(bullets) == 1


def test_docs_separated_bullet_groups_become_separate_lists():
    md = "- a\n- b\n\nprose breaks the run\n\n- c\n- d"
    _, requests = markdown_to_docs(md)
    bullets = [r for r in requests if "createParagraphBullets" in r]
    assert len(bullets) == 2


def test_docs_ordered_lists_are_also_bulleted():
    _, requests = markdown_to_docs("1. first\n2. second")
    assert any("createParagraphBullets" in r for r in requests)


def test_docs_bold_range_covers_exactly_the_bold_words():
    plain, requests = markdown_to_docs("Deploy **right now** please")
    bold = [r for r in requests
            if "updateTextStyle" in r and r["updateTextStyle"]["textStyle"].get("bold")]
    assert len(bold) == 1
    rng = bold[0]["updateTextStyle"]["range"]
    # Doc indices are 1-based; plain text is 0-based
    assert plain[rng["startIndex"] - 1:rng["endIndex"] - 1] == "right now"


def test_docs_all_ranges_stay_inside_the_inserted_text():
    """Every styling index must be valid, or batchUpdate 400s on the whole doc."""
    md = ("# Title\n\nIntro **bold** and `code`.\n\n"
          "- one\n- two\n\n## Section\n\n```\nfenced code\n```\n\nEnd.")
    plain, requests = markdown_to_docs(md)
    limit = len(plain) + 1
    for request in requests[1:]:
        body = next(iter(request.values()))
        rng = body["range"]
        assert 1 <= rng["startIndex"] < rng["endIndex"] <= limit


def test_docs_horizontal_rules_are_dropped():
    plain, _ = markdown_to_docs("Above\n\n---\n\nBelow")
    assert "---" not in plain


def test_docs_fenced_block_keeps_content_but_not_fences():
    plain, _ = markdown_to_docs("```\npip install thing\n```")
    assert "pip install thing" in plain
    assert "```" not in plain


# ── markdown_to_storage ──────────────────────────────────────

def test_storage_converts_heading_to_html():
    assert "<h2>Overview</h2>" in markdown_to_storage("## Overview")


def test_storage_converts_list_to_ul():
    html = markdown_to_storage("- one\n- two")
    assert "<ul>" in html
    assert "<li>one</li>" in html


def test_storage_does_not_wrap_everything_in_one_paragraph():
    """The original bug: the whole document went inside a single <p>."""
    html = markdown_to_storage("# Title\n\nParagraph one.\n\nParagraph two.")
    assert html.count("<p>") >= 2
    assert "# Title" not in html