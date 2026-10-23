"""`docgen eval`: fixtures → models → scores → report, with fake model servers."""
import json

import pytest
import requests

from conftest import make_response
from docgen.cli import main
from docgen.errors import ConfigError
from docgen.eval import ModelSpec, load_fixtures, run_eval
from docgen.eval.report import comparison_table, findings, render
from docgen.eval.runner import FIXTURES_FILE, summarise
from docgen.llm import Completion
from docgen.models import TokenUsage

FILLER = ("This section restates what the ticket says in plain words, with enough words to "
          "count as a real section body and not a placeholder of any kind at all.")
LIST = "- The first point restates the ticket.\n- The second point restates it again."
TABLE = "| Item | Detail |\n|---|---|\n| one | restated from the ticket |"
CODE = "```text\nexample\n```"


def document_from_prompt(prompt: str, sloppy: bool) -> str:
    """Answer every requested section with content in every format, so structure passes."""
    out = []
    for line in prompt.splitlines():
        if line.startswith("## "):
            title = line[3:].split(" (optional")[0]
            if sloppy and title in ("Acceptance Criteria", "Requirements"):
                continue  # drops a required section
            out += [f"## {title}", FILLER, LIST, "1. An ordered step restating the ticket.", TABLE, CODE]
            if sloppy:
                out.append("It caches results in Redis for 7 days.")  # invented specifics
    return "\n\n".join(out)


class FakeModels:
    """openrouter = careful model, ollama = sloppy model."""

    def __init__(self, monkeypatch, judge_reply=None, fail=()):
        self.calls = []
        self.judge_reply = judge_reply
        self.fail = set(fail)
        monkeypatch.setattr(requests, "request", self.request)

    def request(self, method, url, **kwargs):
        messages = kwargs["json"]["messages"]
        self.calls.append((url, messages))
        if messages[0]["content"].startswith("You are a strict fact-checker"):
            return make_response(200, {"choices": [{"message": {"content": self.judge_reply}}],
                                       "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                                 "total_tokens": 15, "cost": 0.0001}})
        sloppy = "11434" in url
        if ("ollama" if sloppy else "openrouter") in self.fail:
            return make_response(400, text="model exploded")
        text = document_from_prompt(messages[1]["content"], sloppy)
        if sloppy:
            return make_response(200, {"message": {"content": text},
                                       "prompt_eval_count": 300, "eval_count": 700})
        return make_response(200, {"choices": [{"message": {"content": text}}],
                                   "usage": {"prompt_tokens": 400, "completion_tokens": 600,
                                             "total_tokens": 1000}})


@pytest.fixture
def models(monkeypatch, openrouter_env):
    return FakeModels(monkeypatch)


# ── fixtures and specs ───────────────────────────────────────

def test_builtin_fixtures_cover_every_builtin_template():
    from docgen.templates import available_templates
    fixtures = load_fixtures()
    assert {f.template for f in fixtures} == set(available_templates())
    assert len({f.id for f in fixtures}) == len(fixtures)


def test_only_filters_and_rejects_unknown_ids():
    assert [f.id for f in load_fixtures(only=["login-jwt"])] == ["login-jwt"]
    with pytest.raises(ConfigError, match="Unknown fixture"):
        load_fixtures(only=["nope"])


@pytest.mark.parametrize("body, message", [
    ("not: a list", "must be a YAML list"),
    ("- id: x\n  template: prd\n", "ticket"),
    ("- [unclosed", "not valid YAML"),
    ("- id: x\n  template: prd\n  ticket: {key: bad key, title: t}\n", "not a valid Jira issue key"),
])
def test_bad_fixture_files(tmp_path, body, message):
    path = tmp_path / "f.yaml"
    path.write_text(body)
    with pytest.raises(ConfigError, match=message):
        load_fixtures(path)


def test_missing_fixture_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_fixtures(tmp_path / "missing.yaml")


def test_model_spec_parsing():
    assert ModelSpec.parse("openrouter") == ModelSpec(backend="openrouter")
    # OpenRouter model ids may themselves contain a colon
    assert ModelSpec.parse("openrouter:meta-llama/llama-3-8b-instruct:free").model == \
        "meta-llama/llama-3-8b-instruct:free"
    with pytest.raises(ConfigError):
        ModelSpec.parse("gpt:4")


def test_model_override_reaches_the_request(models):
    backend = ModelSpec.parse("openrouter:anthropic/claude-test").build()
    assert backend.model == "anthropic/claude-test"


# ── runner ───────────────────────────────────────────────────

def test_careful_model_beats_sloppy_model(models):
    backends = [ModelSpec.parse("openrouter").build(), ModelSpec.parse("ollama").build()]
    report = run_eval(load_fixtures(), backends, price_in=1.0, price_out=2.0)

    careful, sloppy = report.summaries
    assert careful.runs == sloppy.runs == 6 and careful.errors == 0
    assert careful.means["overall"] > sloppy.means["overall"]
    assert careful.means["faithfulness"] == 1.0
    assert sloppy.means["faithfulness"] < 1.0
    assert sloppy.means["completeness"] < 1.0
    # tokens and cost: OpenRouter estimated from prices, Ollama is local (free)
    assert careful.total_tokens == 6000
    assert careful.total_cost_usd == pytest.approx(6 * (400 + 600 * 2) / 1e6)
    assert sloppy.total_cost_usd == 0.0
    # runs interleave models per fixture
    assert [r.model.split(":")[0] for r in report.records[:2]] == ["openrouter", "ollama"]


def test_generation_errors_are_recorded_not_fatal(monkeypatch, openrouter_env):
    FakeModels(monkeypatch, fail=["ollama"])
    backends = [ModelSpec.parse("openrouter").build(), ModelSpec.parse("ollama").build()]
    report = run_eval(load_fixtures(only=["login-jwt"]), backends)
    ok, failed = report.records
    assert ok.error is None and failed.error and "HTTP 400" in failed.error
    assert report.summaries[1].errors == 1 and report.summaries[1].means["overall"] is None


def test_judge_replaces_heuristic_faithfulness(monkeypatch, openrouter_env):
    FakeModels(monkeypatch, judge_reply='{"claims": [{"claim": "a", "supported": true}, '
                                        '{"claim": "b", "supported": false}]}')
    backend = ModelSpec.parse("openrouter").build()
    report = run_eval(load_fixtures(only=["login-jwt"]), [backend], judge=backend)
    record = report.records[0]
    assert record.scores["faithfulness"].value == 0.5
    assert record.scores["faithfulness"].method.startswith("llm-judge")
    assert record.scores["faithfulness_heuristic"].value == 1.0
    assert record.judge_cost_usd == 0.0001
    assert report.judge.startswith("openrouter:")


def test_judge_failure_falls_back_to_heuristic(monkeypatch, openrouter_env):
    FakeModels(monkeypatch, judge_reply="no json here")
    backend = ModelSpec.parse("openrouter").build()
    record = run_eval(load_fixtures(only=["login-jwt"]), [backend], judge=backend).records[0]
    assert record.scores["faithfulness"].method == "entity-grounding"
    assert record.scores["faithfulness_judge"].value is None


def test_bad_template_in_fixture_fails_before_any_call(tmp_path, models):
    path = tmp_path / "f.yaml"
    path.write_text("- id: x\n  template: nope\n  ticket: {key: XY-1, title: t}\n")
    with pytest.raises(ConfigError, match="Unknown template"):
        run_eval(load_fixtures(path), [ModelSpec.parse("openrouter").build()])
    assert models.calls == []


def test_summarise_with_no_successful_runs():
    from docgen.eval.runner import EvalRecord
    summary = summarise("m", [EvalRecord(fixture="f", template="t", model="m", error="x")])
    assert summary.means["overall"] is None and summary.total_tokens is None


# ── report rendering ─────────────────────────────────────────

def test_render_tables(models):
    backends = [ModelSpec.parse("openrouter").build(), ModelSpec.parse("ollama").build()]
    report = run_eval(load_fixtures(only=["login-jwt", "bulk-export-prd"]), backends)

    text = render(report, compare=True, details=True)
    assert "Scores by model" in text and "Side by side" in text and "Findings" in text
    side = comparison_table(report)
    assert side.count("login-jwt") == 1 and "1.00*" in side      # best score marked
    assert "unsupported term: Redis" in findings(report)
    assert "Acceptance Criteria — missing" in findings(report)

    plain = render(report)
    assert "Per run" in plain and "Side by side" not in plain


def test_findings_when_everything_passes():
    from docgen.eval.runner import EvalReport
    report = EvalReport(created_at="now", fixtures_file="f", models=[], records=[], summaries=[])
    assert findings(report) == "No findings — every check passed."


# ── CLI ──────────────────────────────────────────────────────

def test_eval_cli_compare_writes_report_and_docs(models, tmp_path, capsys):
    out = tmp_path / "out" / "report.json"
    code = main(["eval", "--compare", "--only", "login-jwt", "webhooks-api",
                 "--output", str(out), "--save-docs", str(tmp_path / "docs")])
    assert code == 0
    stdout = capsys.readouterr().out
    assert "openrouter:google/gemini-2.0-flash-lite-001" in stdout
    assert "ollama:qwen2.5:latest" in stdout and "Side by side" in stdout

    report = json.loads(out.read_text())
    assert len(report["records"]) == 4
    assert {s["model"] for s in report["summaries"]} == set(report["models"])
    saved = sorted(p.name for p in (tmp_path / "docs").iterdir())
    assert "login-jwt__ollama_qwen2.5_latest.md" in saved


def test_eval_cli_min_score_gate(models):
    assert main(["eval", "--models", "ollama", "--only", "login-jwt", "--min-score", "0.99"]) == 1
    assert main(["eval", "--models", "openrouter", "--only", "login-jwt", "--min-score", "0.9"]) == 0


def test_eval_cli_generation_error_exit_1(monkeypatch, openrouter_env):
    FakeModels(monkeypatch, fail=["openrouter"])
    assert main(["eval", "--only", "login-jwt"]) == 1


def test_eval_cli_with_judge(monkeypatch, openrouter_env, capsys):
    FakeModels(monkeypatch, judge_reply='{"claims": []}')
    assert main(["eval", "--only", "login-jwt", "--judge", "openrouter"]) == 0
    assert "Faithfulness judged by openrouter:" in capsys.readouterr().out


@pytest.mark.parametrize("args, message", [
    (["--compare", "--models", "openrouter"], "at least two models"),
    (["--models", "gpt"], "Unknown backend"),
    (["--only", "nope"], "Unknown fixture"),
])
def test_eval_cli_setup_errors(openrouter_env, capsys, args, message):
    assert main(["eval", *args]) == 2
    assert message in capsys.readouterr().err


def test_eval_cli_requires_only_llm_settings(capsys):
    # no Jira settings needed for eval, but the OpenRouter key is
    assert main(["eval"]) == 2
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY" in err and "JIRA" not in err


def test_fixtures_file_is_packaged():
    assert FIXTURES_FILE.exists()


def test_completion_usage_type():
    assert Completion(text="x", usage=TokenUsage(model="m")).usage.cost(1, 1) is None
