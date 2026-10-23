import pytest

from docgen import config
from docgen.cli import main
from docgen.config import JiraSettings, load
from docgen.errors import ConfigError, InvalidTicketKeyError
from docgen.models import normalise_ticket_key


def test_missing_env_vars_are_named(monkeypatch):
    monkeypatch.setenv("JIRA_DOMAIN", "x.atlassian.net")
    with pytest.raises(ConfigError) as info:
        load(JiraSettings)
    assert "JIRA_EMAIL" in info.value.message
    assert "JIRA_TOKEN" in info.value.message
    assert "JIRA_DOMAIN" not in info.value.message


def test_empty_env_var_counts_as_missing(monkeypatch, jira_env):
    monkeypatch.setenv("JIRA_TOKEN", "")
    with pytest.raises(ConfigError, match="JIRA_TOKEN"):
        load(JiraSettings)


def test_settings_read_from_dotenv_file(tmp_path):
    (tmp_path / ".env").write_text(
        "JIRA_DOMAIN=https://acme.atlassian.net/\nJIRA_EMAIL=a@b.c\nJIRA_TOKEN=supersecret\n")
    settings = load(JiraSettings)
    assert settings.domain == "acme.atlassian.net"  # scheme and slash stripped
    assert settings.token.get_secret_value() == "supersecret"
    assert "supersecret" not in repr(settings)  # secrets never leak into logs


def test_preflight_reports_every_problem_at_once():
    with pytest.raises(ConfigError) as info:
        config.preflight("openrouter", "gdocs")
    msg = info.value.message
    assert "JIRA_TOKEN" in msg and "OPENROUTER_API_KEY" in msg and "credentials.json" in msg


def test_preflight_only_requires_what_the_run_uses(jira_env):
    config.preflight("ollama", "none")  # no OpenRouter key, no Google files: fine


@pytest.mark.parametrize("raw, expected", [
    ("DEMO-2", "DEMO-2"), ("demo-2", "DEMO-2"), (" AB_C-104 ", "AB_C-104")])
def test_valid_ticket_keys(raw, expected):
    assert normalise_ticket_key(raw) == expected


@pytest.mark.parametrize("raw", ["DEMO", "DEMO-", "2-DEMO", "DEMO-0", "DE MO-1", "D-1", "DEMO-1/x"])
def test_invalid_ticket_keys(raw):
    with pytest.raises(InvalidTicketKeyError):
        normalise_ticket_key(raw)


def test_cli_exits_2_on_missing_config(capsys):
    assert main(["DEMO-2"]) == 2
    assert "JIRA_TOKEN" in capsys.readouterr().err


def test_cli_exits_2_on_bad_ticket_key(jira_env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert main(["not a key"]) == 2
