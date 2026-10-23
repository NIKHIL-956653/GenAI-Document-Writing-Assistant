import pytest

from docgen.eval.judge import judge_faithfulness, parse_verdict
from docgen.eval.markdown import matches, normalise_heading, parse
from docgen.eval.scorers import completeness, extract_specifics, faithfulness, structure
from docgen.llm import Completion
from docgen.models import Ticket, TokenUsage
from docgen.templates import SectionSpec, load_template

TICKET = Ticket(
    key="EVAL-1", title="Build secure login system with JWT", labels=["auth"],
    description="Users log in with email and password at POST /api/login. Access tokens "
                "expire after 15 minutes. Lock the account after 5 failed attempts.",
)

PROSE = ("This paragraph explains the change in plain words and has more than enough words "
         "to count as a real, non-trivial section body for the scorer, which wants at least "
         "thirty words here.")
BULLETS = "\n".join(f"- The {n} item describes one concrete piece of the work in a few words."
                    for n in ("first", "second", "third"))
STEPS = "\n".join(f"{n}. Step {n} is one concrete, ordered action with a few words." for n in range(1, 4))

GOOD = f"""## Overview

{PROSE}

## Technical Details

The `POST /api/login` endpoint accepts email and password and returns a JWT. Access tokens expire
after 15 minutes, and the account is locked after 5 failed attempts. {PROSE}

## Implementation Steps

{STEPS}

## Acceptance Criteria

{BULLETS}

## Known Limitations

- The refresh token lifetime (for example 30 days) is not specified in the ticket.
- Rate limiting via Cloudflare is not mentioned.
"""


@pytest.fixture
def technical():
    return load_template("technical")


# ── markdown parsing ─────────────────────────────────────────

def test_parse_nests_subsections_and_ignores_headings_in_code():
    doc = parse("intro\n## A\ntext\n### A.1\nmore\n```\n## not a heading\n```\n## B\nend")
    assert doc.preamble == ["intro"]
    assert [(s.level, s.heading) for s in doc.sections] == [(2, "A"), (3, "A.1"), (2, "B")]
    assert "more" in doc.sections[0].body          # h3 belongs to the h2 above it
    assert "## not a heading" in doc.sections[0].body
    assert not doc.unclosed_fence


def test_unclosed_fence_detected():
    assert parse("## A\n```\ncode").unclosed_fence


@pytest.mark.parametrize("heading", ["Overview", "1. Overview", "**Overview:**", "Section 1: Overview",
                                     "overview", "Overview of the change"])
def test_heading_matching_tolerates_numbering_and_emphasis(heading):
    assert matches(SectionSpec(title="Overview"), heading)


def test_heading_matching_uses_aliases_and_is_not_too_loose():
    spec = SectionSpec(title="Request and Response Examples", aliases=["Examples"])
    assert matches(spec, "Request & Response Examples")
    assert matches(spec, "Examples")
    assert not matches(SectionSpec(title="Goals"), "Non-Goals")
    assert normalise_heading("2) What’s New") == "what's new".replace("'", " ").replace("  ", " ")


# ── faithfulness ─────────────────────────────────────────────

def test_grounded_document_scores_one(technical):
    score = faithfulness(GOOD, TICKET, technical)
    assert score.value == 1.0, score.notes


def test_unknowns_section_is_exempt(technical):
    # "30 days" and "Cloudflare" appear only under Known Limitations
    assert faithfulness(GOOD, TICKET, technical).value == 1.0


def test_invented_specifics_are_flagged(technical):
    doc = GOOD.replace("## Implementation Steps", "Tokens are signed with RS256 and cached in Redis "
                       "for 7 days using `TokenCache`. See https://jwt.io.\n\n## Implementation Steps")
    score = faithfulness(doc, TICKET, technical)
    unsupported = " ".join(score.notes)
    for invented in ("RS256", "Redis", "number: 7", "TokenCache", "https://jwt.io"):
        assert invented in unsupported
    assert 0 < score.value < 1


def test_invented_endpoint_path_is_flagged(technical):
    doc = GOOD.replace("## Acceptance Criteria",
                       "Call /api/v2/logout to end a session.\n\n## Acceptance Criteria")
    assert "path: /api/v2/logout" in " ".join(faithfulness(doc, TICKET, technical).notes)


def test_generic_vocabulary_and_status_codes_are_allowed(technical):
    doc = ("## Overview\n\nThe HTTP API returns JSON, and responds 401 on bad credentials "
           "or 429 when throttled.")
    assert faithfulness(doc, TICKET, technical).value == 1.0


def test_document_without_specifics_scores_one(technical):
    assert faithfulness("## Overview\n\nplain words only here", TICKET, technical).value == 1.0


def test_extract_specifics_skips_code_blocks_except_paths():
    found = extract_specifics('```json\n{"retries": 3, "url": "/v1/items"}\n```\nStep 4 runs.')
    assert ("path", "/v1/items") in found
    assert ("number", "3") not in found      # example payload values are illustrative
    assert ("number", "4") not in found      # "Step 4" is a counter, not a claim


def test_placeholders_are_not_claims():
    found = extract_specifics("Call GET /users/<user-id> with {token}.")
    assert found == [("path", "/users"), ("term", "GET")]  # GET is then allowed as generic


# ── completeness ─────────────────────────────────────────────

def test_complete_document(technical):
    score = completeness(GOOD, technical)
    assert score.value == 1.0
    assert all(c.passed for c in score.checks)


def test_missing_section_scores_zero_for_it(technical):
    doc = GOOD.replace("## Acceptance Criteria", "## Something Else")
    score = completeness(doc, technical)
    assert score.value == pytest.approx(4 / 5)
    assert any(c.name == "Acceptance Criteria" and c.detail == "missing" for c in score.checks)


@pytest.mark.parametrize("body", ["TBD", "N/A", "To be determined.", "Short."])
def test_trivial_section_gets_half_credit(technical, body):
    doc = GOOD.replace(PROSE + "\n\n## Technical", body + "\n\n## Technical", 1)
    score = completeness(doc, technical)
    assert score.value == pytest.approx(4.5 / 5)


def test_min_words_zero_accepts_none(tmp_path):
    template = load_template("release_notes")
    doc = ("## Summary\n\n" + PROSE + "\n\n## What's New\n\n" + BULLETS
           + "\n\n## Breaking Changes\n\nNone.\n\n## Known Issues\n\n" + BULLETS)
    score = completeness(doc, template)
    assert score.value == 1.0  # optional Upgrade Notes absent is fine
    assert any(c.name == "Upgrade Notes" and c.detail == "optional, absent" for c in score.checks)


# ── structure ────────────────────────────────────────────────

def test_well_structured_document(technical):
    score = structure(GOOD, technical)
    assert score.value == 1.0, [c for c in score.checks if not c.passed]


def failed(score):
    return {c.name for c in score.checks if not c.passed}


def test_preamble_is_flagged(technical):
    assert "no_preamble" in failed(structure("Sure! Here is your document.\n\n" + GOOD, technical))


def test_wrong_heading_level_is_flagged(technical):
    assert "heading_level" in failed(structure(GOOD.replace("## ", "# "), technical))


def test_out_of_order_sections_are_flagged(technical):
    a, b = GOOD.split("## Implementation Steps")
    steps, rest = b.split("## Acceptance Criteria")
    swapped = a + "## Acceptance Criteria" + rest.split("## Known")[0] + "## Implementation Steps" \
        + steps + "## Known" + rest.split("## Known")[1]
    assert "section_order" in failed(structure(swapped, technical))


def test_duplicate_sections_and_unclosed_fences(technical):
    doc = GOOD + "\n## Overview\n\nagain\n\n```\nunterminated"
    assert {"no_duplicate_sections", "code_fences_closed"} <= failed(structure(doc, technical))


def test_section_format_expectations(technical):
    doc = GOOD.replace(BULLETS, PROSE)  # Acceptance Criteria must be a list
    assert "format:Acceptance Criteria" in failed(structure(doc, technical))


def test_table_and_code_formats():
    api = load_template("api_docs")
    doc = ("## Overview\n\n" + PROSE + "\n\n## Authentication\n\nBearer key.\n\n## Endpoints\n\n"
           "| Method | Path |\n|---|---|\n| GET | /v1/webhooks |\n\n## Request and Response Examples\n\n"
           "```http\nGET /v1/webhooks\n```\n\n## Errors\n\n- 422 invalid url\n\n## Open Questions\n\n- none")
    assert structure(doc, api).value == 1.0
    broken = doc.replace("|---|---|", "").replace("```http\nGET /v1/webhooks\n```", "see above")
    assert {"format:Endpoints", "format:Request and Response Examples"} <= failed(structure(broken, api))


def test_output_schema_extra_sections_and_length(tmp_path):
    from docgen.templates import parse_template
    path = tmp_path / "strict.yaml"
    path.write_text("title: S\nsystem_prompt: P\nsections:\n  - title: A\n"
                    "output:\n  allow_extra_sections: false\n  max_words: 5\n")
    template = parse_template(path)
    score = structure("## A\n\none two three four five six\n\n## Bonus\n\nx", template)
    assert {"no_extra_sections", "max_words"} <= failed(score)


# ── LLM judge ────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    '{"claims": [{"claim": "a", "supported": true}]}',
    '```json\n{"claims": [{"claim": "a", "supported": true}]}\n```',
    'Here you go: {"claims": [{"claim": "a", "supported": true}]} hope that helps',
])
def test_parse_verdict_tolerates_wrapping(text):
    assert parse_verdict(text).claims[0].claim == "a"


def test_parse_verdict_rejects_garbage():
    with pytest.raises(ValueError):
        parse_verdict("I think it is fine")


class FakeJudge:
    name, model = "fake", "judge-1"

    def __init__(self, text=None, error=None):
        self.text, self.error, self.messages = text, error, None

    def complete(self, messages):
        self.messages = messages
        if self.error:
            raise self.error
        return Completion(text=self.text, usage=TokenUsage(model="judge-1", total_tokens=10,
                                                           cost_usd=0.001))


def test_judge_scores_supported_fraction():
    judge = FakeJudge('{"claims": [{"claim": "uses JWT", "supported": true},'
                      ' {"claim": "uses Redis", "supported": false, "reason": "not in ticket"}]}')
    score, usage = judge_faithfulness(judge, GOOD, TICKET)
    assert score.value == 0.5
    assert score.method == "llm-judge:fake/judge-1"
    assert "unsupported: uses Redis (not in ticket)" in score.notes
    assert usage.cost_usd == 0.001
    assert "email and password" in judge.messages[1]["content"]


def test_judge_failure_yields_no_score():
    from docgen.errors import TransientError
    score, usage = judge_faithfulness(FakeJudge(error=TransientError("down", service="x")), GOOD, TICKET)
    assert score.value is None and usage is None
    score, _ = judge_faithfulness(FakeJudge("not json"), GOOD, TICKET)
    assert score.value is None
