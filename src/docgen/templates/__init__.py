"""
Document templates: PRD, runbook, release notes, API docs, postmortem, ...

A template is a YAML file that defines three things:

- the prompt (`system_prompt`, optional `instructions` / `user_prompt`),
- the section structure the model must produce (`sections`), and
- the output schema the result is checked against (`sections[*].format`,
  `min_words`, `required`, plus the `output` block) — the same schema the
  evaluation harness scores structure and completeness against.

Templates are looked up by name in, highest priority first:

1. `--templates-dir` / `DOCGEN_TEMPLATES_DIR`
2. `./templates/` in the working directory
3. the templates shipped inside this package

so adding a document type, or overriding a built-in one, is a YAML file
and no code change.
"""
from __future__ import annotations

import os
import re
from enum import Enum
from pathlib import Path
from string import Template as _StringTemplate

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from docgen.errors import ConfigError

BUILTIN_DIR = Path(__file__).parent
DEFAULT_TEMPLATE = "technical"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class Format(str, Enum):
    """What a section's body must contain to be structurally valid."""
    ANY = "any"
    PROSE = "prose"
    LIST = "list"                    # bulleted or numbered
    ORDERED_LIST = "ordered_list"
    TABLE = "table"
    CODE = "code"                    # fenced code block


FORMAT_INSTRUCTIONS = {
    Format.PROSE: "write in paragraphs",
    Format.LIST: "use a bulleted list",
    Format.ORDERED_LIST: "use a numbered list",
    Format.TABLE: "use a Markdown table",
    Format.CODE: "include fenced code blocks",
}


class SectionSpec(BaseModel):
    title: str = Field(min_length=1)
    guidance: str = ""
    required: bool = True
    aliases: list[str] = Field(default_factory=list)
    # Any one of these satisfies the structure check; ANY disables it.
    format: list[Format] = Field(default_factory=lambda: [Format.ANY])
    # Below this many words a present section counts as trivial. 0 disables the check
    # (e.g. "Breaking Changes: None." is a legitimate answer).
    min_words: int = Field(default=20, ge=0)
    # The section where the model must put what the ticket does not specify.
    # Faithfulness scoring skips it: saying "X is not specified" is not a claim.
    unknowns: bool = False

    @field_validator("format", mode="before")
    @classmethod
    def _one_or_many(cls, value):
        return [value] if isinstance(value, str) else value


class OutputSpec(BaseModel):
    heading_level: int = Field(default=2, ge=1, le=6)
    allow_extra_sections: bool = True
    max_words: int | None = Field(default=None, gt=0)


class Template(BaseModel):
    name: str
    title: str
    description: str = ""
    system_prompt: str = Field(min_length=1)
    instructions: str = ""
    # Optional full override of the user message. `$`-placeholders:
    # $key $title $description $status $labels $context $images $sections $instructions
    user_prompt: str | None = None
    sections: list[SectionSpec] = Field(min_length=1)
    output: OutputSpec = Field(default_factory=OutputSpec)
    source: Path | None = Field(default=None, exclude=True)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not NAME_RE.match(value):
            raise ValueError("must be lowercase letters, digits, '-' or '_'")
        return value

    @model_validator(mode="after")
    def _unique_sections(self):
        titles = [s.title.lower() for s in self.sections]
        if len(titles) != len(set(titles)):
            raise ValueError("section titles must be unique")
        if sum(s.unknowns for s in self.sections) > 1:
            raise ValueError("at most one section may set unknowns: true")
        return self

    @property
    def unknowns_section(self) -> SectionSpec | None:
        return next((s for s in self.sections if s.unknowns), None)

    def section_outline(self) -> str:
        """The section list as it is shown to the model."""
        marker = "#" * self.output.heading_level
        lines = []
        for spec in self.sections:
            notes = [FORMAT_INSTRUCTIONS[f] for f in spec.format if f in FORMAT_INSTRUCTIONS]
            line = f"{marker} {spec.title}"
            if not spec.required:
                line += " (optional — omit if the ticket gives nothing for it)"
            detail = " ".join(filter(None, [spec.guidance.strip(),
                                            "Format: " + " or ".join(notes) + "." if notes else ""]))
            if detail:
                line += f"\n   {detail}"
            lines.append(line)
        return "\n".join(lines)


# ── discovery ────────────────────────────────────────────────

def search_dirs(extra: Path | None = None) -> list[Path]:
    dirs = []
    if extra is not None:
        dirs.append(extra)
    env_dir = os.getenv("DOCGEN_TEMPLATES_DIR")
    if env_dir:
        dirs.append(Path(env_dir))
    dirs.append(Path.cwd() / "templates")
    dirs.append(BUILTIN_DIR)
    return dirs


def _files(extra: Path | None) -> dict[str, Path]:
    """name → file, earlier search dirs winning."""
    found: dict[str, Path] = {}
    for directory in search_dirs(extra):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.y*ml")):
            if path.suffix in (".yaml", ".yml"):
                found.setdefault(path.stem, path)
    return found


def available_templates(extra: Path | None = None) -> list[str]:
    return sorted(_files(extra))


def parse_template(path: Path) -> Template:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Template {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Template {path} must be a YAML mapping")
    data.setdefault("name", path.stem)
    try:
        template = Template(**data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ConfigError(f"Template {path} is invalid: {problems}",
                          hint="See docs/templates.md for the template schema") from None
    template.source = path
    return template


def load_template(name: str, extra: Path | None = None) -> Template:
    files = _files(extra)
    if name not in files:
        raise ConfigError(f"Unknown template '{name}'",
                          hint="Available: " + ", ".join(sorted(files)))
    return parse_template(files[name])


IMAGE_INSTRUCTION = ("These images from the ticket can be embedded. To place one where it helps the "
                     "reader, write ![short caption](attachment:FILENAME) on its own line, using a "
                     "filename from this list exactly: {names}")


def render_user_prompt(template: Template, *, key: str, title: str, description: str,
                       status: str, labels: list[str], context: str = "",
                       images: list[str] | None = None) -> str:
    image_note = IMAGE_INSTRUCTION.format(names=", ".join(images)) if images else ""
    values = {
        "key": key, "title": title, "description": description, "status": status,
        "labels": ", ".join(labels) or "none",
        "context": context,
        "images": image_note,
        "sections": template.section_outline(),
        "instructions": template.instructions.strip(),
    }
    if template.user_prompt:
        return _StringTemplate(template.user_prompt).safe_substitute(values)

    parts = [
        f"Write a {template.title} for this Jira ticket.",
        "",
        f"TICKET ID: {key}",
        f"TITLE: {title}",
        f"DESCRIPTION: {description}",
        f"STATUS: {status}",
        f"LABELS: {values['labels']}",
        *(["", "CONTEXT (comments, related issues and attachments; treat as part of the ticket):",
           context] if context else []),
        "",
        "Use exactly these sections, as Markdown headings at the level shown, in this order:",
        "",
        values["sections"],
        "",
    ]
    if values["instructions"]:
        parts += [values["instructions"], ""]
    if image_note:
        parts += [image_note, ""]
    parts.append("Start directly with the first section heading: no title line, no preamble, "
                 "no closing remarks. Output Markdown only.")
    return "\n".join(parts)
