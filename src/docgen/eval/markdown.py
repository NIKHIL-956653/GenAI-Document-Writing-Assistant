"""Split a generated Markdown document into headed sections and match them to a template."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from docgen.templates import SectionSpec, Template

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
BULLET_RE = re.compile(r"^\s*[-*+]\s+\S")
ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+\S")
TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
# "1. Overview", "Section 2: Goals", "**Overview:**"
NUMBERING_RE = re.compile(r"^(?:section\s+)?\d+(?:\.\d+)*[.):]?\s+", re.IGNORECASE)
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’_-]*")


@dataclass
class Section:
    level: int
    heading: str
    line: int
    body_lines: list[str] = field(default_factory=list)

    @property
    def body(self) -> str:
        return "\n".join(self.body_lines).strip()

    @property
    def words(self) -> int:
        return count_words(self.body)

    def has_bullets(self) -> bool:
        return any(BULLET_RE.match(line) or ORDERED_RE.match(line)
                   for line in _outside_fences(self.body_lines))

    def has_ordered_list(self) -> bool:
        return any(ORDERED_RE.match(line) for line in _outside_fences(self.body_lines))

    def has_table(self) -> bool:
        return any(TABLE_SEPARATOR_RE.match(line) for line in _outside_fences(self.body_lines))

    def has_code_block(self) -> bool:
        return sum(1 for line in self.body_lines if FENCE_RE.match(line)) >= 2

    def has_prose(self) -> bool:
        for line in _outside_fences(self.body_lines):
            text = line.strip()
            if (text and not BULLET_RE.match(line) and not ORDERED_RE.match(line)
                    and not text.startswith("|") and count_words(text) >= 5):
                return True
        return False


@dataclass
class ParsedDocument:
    preamble: list[str]
    sections: list[Section]
    unclosed_fence: bool

    def at_level(self, level: int) -> list[Section]:
        return [s for s in self.sections if s.level == level]


def _outside_fences(lines: list[str]):
    in_fence = False
    for line in lines:
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield line


def count_words(text: str) -> int:
    return len(WORD_RE.findall(text))


def parse(markdown: str) -> ParsedDocument:
    """
    Every heading opens a section that runs to the next heading of the same or
    a higher level, so a `##` section's body includes its `###` subsections.
    """
    lines = markdown.splitlines()
    headings: list[tuple[int, int, str]] = []    # (line index, level, text)
    in_fence = False
    for i, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and (m := HEADING_RE.match(line)):
            headings.append((i, len(m.group(1)), m.group(2)))

    sections = []
    for n, (i, level, text) in enumerate(headings):
        end = next((j for j, lvl, _ in headings[n + 1:] if lvl <= level), len(lines))
        sections.append(Section(level=level, heading=text, line=i + 1,
                                body_lines=lines[i + 1:end]))
    first = headings[0][0] if headings else len(lines)
    return ParsedDocument(preamble=lines[:first], sections=sections, unclosed_fence=in_fence)


def normalise_heading(text: str) -> str:
    text = text.strip().strip("*_ ").rstrip(":").strip("*_ ")
    text = NUMBERING_RE.sub("", text)
    text = text.replace("&", " and ").replace("’", "'").lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def matches(spec: SectionSpec, heading: str) -> bool:
    norm = normalise_heading(heading)
    names = [normalise_heading(n) for n in [spec.title, *spec.aliases]]
    # "Overview" also matches "Overview of the change"; never the other way round
    return any(norm == n or norm.startswith(n + " ") for n in names)


def find_section(doc: ParsedDocument, spec: SectionSpec, level: int | None = None) -> Section | None:
    """First section matching the spec, preferring the template's heading level."""
    candidates = [s for s in doc.sections if matches(spec, s.heading)]
    if level is not None:
        at_level = [s for s in candidates if s.level == level]
        if at_level:
            return at_level[0]
    return candidates[0] if candidates else None


def match_template(doc: ParsedDocument, template: Template) -> dict[str, Section | None]:
    level = template.output.heading_level
    return {spec.title: find_section(doc, spec, level) for spec in template.sections}
