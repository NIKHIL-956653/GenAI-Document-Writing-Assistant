import pytest

from docgen.errors import ConfigError
from docgen.llm import build_messages, system_prompt
from docgen.models import Ticket
from docgen.templates import (
    BUILTIN_DIR,
    DEFAULT_TEMPLATE,
    Format,
    available_templates,
    load_template,
    parse_template,
)

BUILTINS = ["api_docs", "postmortem", "prd", "release_notes", "runbook", "technical"]


@pytest.fixture
def ticket():
    return Ticket(key="DEMO-1", title="Build secure login system with JWT",
                  description="Users need to authenticate with email and password.",
                  status="To Do", labels=["auth", "backend"])


def write(directory, name, body):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(body)
    return path


MINIMAL = """
title: Minimal
system_prompt: You write things.
sections:
  - title: Body
"""


# ── built-ins ────────────────────────────────────────────────

def test_all_builtin_templates_are_listed():
    assert available_templates() == BUILTINS


@pytest.mark.parametrize("name", BUILTINS)
def test_builtin_template_is_valid(name):
    template = load_template(name)
    assert template.name == name
    assert template.source.parent == BUILTIN_DIR
    # every built-in has exactly one place for unknowns, so faithfulness has an outlet
    assert template.unknowns_section is not None
    assert any(s.required for s in template.sections)


@pytest.mark.parametrize("name", BUILTINS)
def test_prompt_contains_ticket_sections_and_faithfulness_rule(name, ticket):
    template = load_template(name)
    system, user = build_messages(ticket, template)
    assert "Only state facts that are in the ticket" in system["content"]
    assert template.unknowns_section.title in system["content"]
    for field in ("DEMO-1", "Build secure login", "email and password", "To Do", "auth, backend"):
        assert field in user["content"]
    for spec in template.sections:
        assert f"## {spec.title}" in user["content"]


def test_default_template_keeps_the_original_five_sections():
    titles = [s.title for s in load_template(DEFAULT_TEMPLATE).sections]
    assert titles == ["Overview", "Technical Details", "Implementation Steps",
                      "Acceptance Criteria", "Known Limitations"]


def test_missing_labels_render_as_none(ticket):
    ticket = ticket.model_copy(update={"labels": []})
    assert "LABELS: none" in build_messages(ticket, load_template("technical"))[1]["content"]


def test_optional_sections_and_formats_are_explained_to_the_model(ticket):
    user = build_messages(ticket, load_template("prd"))[1]["content"]
    assert "## Non-Goals (optional" in user
    assert "Format: use a bulleted list." in user


# ── user templates ───────────────────────────────────────────

def test_new_template_needs_no_code(tmp_path, ticket):
    write(tmp_path / "templates", "adr", """
title: Architecture Decision Record
description: One decision, its context and consequences.
system_prompt: You are a staff engineer writing an ADR.
sections:
  - title: Context
  - title: Decision
    format: prose
  - title: Consequences
    format: [list, prose]
  - title: Open Questions
    unknowns: true
    min_words: 0
""")
    assert "adr" in available_templates()
    template = load_template("adr")
    assert template.sections[2].format == [Format.LIST, Format.PROSE]
    assert "## Decision" in build_messages(ticket, template)[1]["content"]


def test_precedence_extra_dir_then_env_then_cwd_then_builtin(tmp_path, monkeypatch):
    write(tmp_path / "templates", "prd", MINIMAL.replace("Minimal", "From cwd"))
    assert load_template("prd").title == "From cwd"
    write(tmp_path / "envdir", "prd", MINIMAL.replace("Minimal", "From env"))
    monkeypatch.setenv("DOCGEN_TEMPLATES_DIR", str(tmp_path / "envdir"))
    assert load_template("prd").title == "From env"
    write(tmp_path / "flag", "prd", MINIMAL.replace("Minimal", "From flag"))
    assert load_template("prd", tmp_path / "flag").title == "From flag"


def test_user_prompt_override_uses_dollar_placeholders(tmp_path, ticket):
    write(tmp_path / "templates", "custom", MINIMAL + """
user_prompt: |
  Ticket $key: $title. JSON stays literal: {"a": 1}
  $sections
""")
    user = build_messages(ticket, load_template("custom"))[1]["content"]
    assert user.startswith('Ticket DEMO-1: Build secure login system with JWT. JSON stays literal: {"a": 1}')
    assert "## Body" in user


def test_instructions_are_appended(ticket):
    user = build_messages(ticket, load_template("api_docs"))[1]["content"]
    assert "Put endpoint paths and field names in `backticks`." in user


def test_template_without_unknowns_section_still_gets_rule(tmp_path):
    template = parse_template(write(tmp_path, "m", MINIMAL))
    assert "an explicit note" in system_prompt(template)


# ── invalid templates fail with a clear message ──────────────

@pytest.mark.parametrize("body, message", [
    ("title: [unclosed", "not valid YAML"),
    ("- just\n- a list\n", "must be a YAML mapping"),
    ("title: X\nsystem_prompt: Y\nsections: []\n", "sections"),
    ("title: X\nsections:\n  - title: A\n", "system_prompt"),
    (MINIMAL + "  - title: body\n", "section titles must be unique"),
    ("title: X\nsystem_prompt: Y\nsections:\n  - {title: A, unknowns: true}\n"
     "  - {title: B, unknowns: true}\n", "at most one section"),
    (MINIMAL.replace("  - title: Body", "  - title: Body\n    format: poetry"), "format"),
    (MINIMAL.replace("  - title: Body", "  - title: Body\n    min_words: -1"), "min_words"),
])
def test_invalid_template_raises_config_error(tmp_path, body, message):
    with pytest.raises(ConfigError, match=message):
        parse_template(write(tmp_path, "bad", body))


def test_invalid_template_name(tmp_path):
    with pytest.raises(ConfigError, match="lowercase"):
        parse_template(write(tmp_path, "Bad Name", MINIMAL))


def test_unknown_template_lists_available():
    with pytest.raises(ConfigError) as info:
        load_template("nope")
    assert "runbook" in info.value.hint
