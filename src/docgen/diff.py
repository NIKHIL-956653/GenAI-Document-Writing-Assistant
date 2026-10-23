"""What changed between the stored version of a document and the new one."""
from __future__ import annotations

import difflib

from docgen.eval.markdown import normalise_heading, parse
from docgen.models import ChangeSummary

MAX_DIFF_CHARS = 20_000


def _sections(markdown: str) -> dict[str, tuple[str, str]]:
    """
    normalised heading → (heading as written, body), for the top heading level —
    skipping a lone title heading (`# Ticket title`), which would otherwise make
    the whole document a single section.
    """
    doc = parse(markdown)
    if not doc.sections:
        return {}
    levels = sorted({s.level for s in doc.sections})
    top = levels[0]
    if len(doc.at_level(top)) == 1 and len(levels) > 1:
        top = levels[1]
    out: dict[str, tuple[str, str]] = {}
    for section in doc.at_level(top):
        out.setdefault(normalise_heading(section.heading), (section.heading, section.body))
    return out


def summarize_changes(old: str, new: str, previous_version: int = 1) -> ChangeSummary:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    diff_lines = list(difflib.unified_diff(old_lines, new_lines, fromfile=f"v{previous_version}",
                                           tofile=f"v{previous_version + 1}", lineterm=""))
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))

    before, after = _sections(old), _sections(new)
    diff_text = "\n".join(diff_lines)
    if len(diff_text) > MAX_DIFF_CHARS:
        diff_text = diff_text[:MAX_DIFF_CHARS] + "\n… diff truncated"
    return ChangeSummary(
        previous_version=previous_version,
        added_sections=[after[k][0] for k in after if k not in before],
        removed_sections=[before[k][0] for k in before if k not in after],
        changed_sections=[after[k][0] for k in after if k in before and before[k][1] != after[k][1]],
        lines_added=added, lines_removed=removed, diff=diff_text,
    )


def colorize(diff: str) -> str:
    colors = {"+": "\033[32m", "-": "\033[31m", "@": "\033[36m"}
    out = []
    for line in diff.splitlines():
        color = "" if line.startswith(("+++", "---")) else colors.get(line[:1], "")
        out.append(f"{color}{line}\033[0m" if color else line)
    return "\n".join(out)
