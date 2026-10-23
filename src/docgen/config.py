"""
Typed configuration, loaded from the environment and `.env`.

Each integration has its own settings class so a run only demands the
variables it actually needs: `--llm ollama` does not require an OpenRouter
key, and Google credentials only matter with `--publish gdocs`.

`load()` turns pydantic's validation errors into a ConfigError that names the
exact environment variables to set, and `preflight()` checks everything a
run will need *before* the first API call, so a missing Confluence setting
surfaces in one second rather than after paying for LLM tokens.
"""
from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from docgen.errors import ConfigError

ENV_HINT = "Add them to a .env file in the directory you run docgen from, or export them in your shell."


class _Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore",
    )


class JiraSettings(_Settings):
    model_config = SettingsConfigDict(env_prefix="JIRA_")

    domain: str = Field(min_length=1, description="e.g. yourname.atlassian.net")
    email: str = Field(min_length=1)
    token: SecretStr = Field(min_length=1)
    timeout: float = 30.0

    @field_validator("domain")
    @classmethod
    def _normalise_domain(cls, value: str) -> str:
        value = value.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
        if "." not in value:
            raise ValueError("expected a host like yourname.atlassian.net")
        return value

    @property
    def base_url(self) -> str:
        return f"https://{self.domain}"


class OpenRouterSettings(_Settings):
    model_config = SettingsConfigDict(env_prefix="OPENROUTER_")

    api_key: SecretStr = Field(min_length=1)
    model: str = "google/gemini-2.0-flash-lite-001"
    base_url: str = "https://openrouter.ai/api/v1"
    timeout: float = 120.0


class OllamaSettings(_Settings):
    model_config = SettingsConfigDict(env_prefix="OLLAMA_")

    host: str = "http://localhost:11434"
    model: str = "qwen2.5:latest"
    # Local models on CPU are slow; a long read timeout is expected here.
    timeout: float = 600.0


class ConfluenceSettings(_Settings):
    """Confluence reuses the Jira credentials; only the space is its own."""
    model_config = SettingsConfigDict(env_prefix="CONFLUENCE_")

    space_key: str = "KAN"
    timeout: float = 60.0


class GoogleSettings(_Settings):
    model_config = SettingsConfigDict(env_prefix="GOOGLE_")

    credentials_file: Path = Path("credentials.json")
    token_file: Path = Path("token.json")
    timeout: float = 60.0


S = TypeVar("S", bound=BaseSettings)


def _env_name(settings_cls: type[BaseSettings], field: str) -> str:
    prefix = settings_cls.model_config.get("env_prefix", "")
    return f"{prefix}{field}".upper()


def load(settings_cls: type[S]) -> S:
    """Instantiate a settings class, translating validation errors into a ConfigError."""
    try:
        return settings_cls()
    except ValidationError as exc:
        missing, invalid = [], []
        for err in exc.errors():
            name = _env_name(settings_cls, str(err["loc"][0]))
            if err["type"] in ("missing", "string_too_short"):
                missing.append(name)
            else:
                invalid.append(f"{name}: {err['msg']}")
        parts = []
        if missing:
            parts.append("Missing required environment variable(s): " + ", ".join(missing))
        if invalid:
            parts.append("Invalid environment variable(s): " + "; ".join(invalid))
        raise ConfigError(". ".join(parts), hint=ENV_HINT) from None


def _problems(settings_classes: list[type[BaseSettings]]) -> list[str]:
    problems = []
    for settings_cls in dict.fromkeys(settings_classes):  # de-duplicated, order kept
        try:
            load(settings_cls)
        except ConfigError as exc:
            problems.append(exc.message)
    return problems


def _raise_if(problems: list[str]) -> None:
    if problems:
        raise ConfigError("Configuration problems:\n  - " + "\n  - ".join(problems),
                          hint=ENV_HINT)


def llm_settings(backend: str) -> type[BaseSettings]:
    return OpenRouterSettings if backend == "openrouter" else OllamaSettings


def preflight(llm: str, publish: str) -> None:
    """
    Validate every setting this run needs, and report all problems at once.
    Raises ConfigError; returns None when the run is good to go.
    """
    needed: list[type[BaseSettings]] = [JiraSettings, llm_settings(llm)]
    if publish == "confluence":
        needed.append(ConfluenceSettings)
    problems = _problems(needed)

    if publish == "gdocs":
        try:
            google = load(GoogleSettings)
        except ConfigError as exc:
            problems.append(exc.message)
        else:
            if not google.token_file.exists() and not google.credentials_file.exists():
                problems.append(
                    f"Google OAuth client file not found at {google.credentials_file.resolve()}. "
                    "Download an OAuth 2.0 'Desktop app' client from Google Cloud Console "
                    "(APIs & Services → Credentials) and save it there, or set GOOGLE_CREDENTIALS_FILE"
                )
    _raise_if(problems)


def preflight_backends(backends: list[str]) -> None:
    """For `docgen eval`: only the LLM settings matter (fixtures replace Jira)."""
    _raise_if(_problems([llm_settings(b) for b in backends]))


class DocgenSettings(_Settings):
    """docgen's own settings: the store, and the HTTP server's defaults (`docgen serve`)."""
    model_config = SettingsConfigDict(env_prefix="DOCGEN_")

    store: Path = Path(".docgen") / "docgen.db"
    output_dir: Path = Path(".")
    # server: what a request gets when it doesn't say
    llm: str = "openrouter"
    model: str | None = None
    template: str = "technical"
    context: str = "standard"
    concurrency: int = Field(default=4, ge=1, le=32)
    workers: int = Field(default=2, ge=1, le=16)    # jobs running at once
    api_token: SecretStr | None = None              # Bearer token for the API; unset = open
    # Jira webhook: generate when an issue moves into one of these statuses
    webhook_secret: SecretStr | None = None
    webhook_statuses: str = "Done"                  # comma-separated, case-insensitive
    webhook_template: str | None = None             # default: `template`
    webhook_publish: str = "none"                   # none | gdocs | confluence
    webhook_public: bool = False

    @field_validator("llm")
    @classmethod
    def _known_llm(cls, value: str) -> str:
        if value not in ("openrouter", "ollama"):
            raise ValueError("must be openrouter or ollama")
        return value

    @field_validator("webhook_publish")
    @classmethod
    def _known_target(cls, value: str) -> str:
        if value not in ("none", "gdocs", "confluence"):
            raise ValueError("must be none, gdocs or confluence")
        return value

    @property
    def statuses(self) -> set[str]:
        return {s.strip().casefold() for s in self.webhook_statuses.split(",") if s.strip()}
