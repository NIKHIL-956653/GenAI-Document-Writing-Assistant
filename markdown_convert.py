"""
Markdown → Google Docs requests, and Markdown → Confluence storage XHTML.

Why this exists: the LLM writes Markdown, but neither publishing target
understands it. Google Docs has no markdown importer in the API — you insert
plain text, then apply styling by character range. Confluence wants XHTML.
Publishing raw markdown to either produces a document full of literal
"## " and "- " characters, which is what the first version did.

Both functions here are pure (no network), so they are unit-testable.
"""
import html
import re

# ── Google Docs ──────────────────────────────────────────────

HEADING_STYLES = {
    1: "HEADING_1", 2: "HEADING_2", 3: "HEADING_3",
    4: "HEADING_4", 5: "HEADING_5", 6: "HEADING_6",
}

BULLET_RE = re.compile(r"^[-*+]\s+")
ORDERED_RE = re.compile(r"^\d+[.)]\s+")
HR_RE = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")


def _parse_inline(text: str, offset: int):
    """
    Strip **bold** and `code` markers, returning the clean text plus the
    character ranges that need styling. Offsets are absolute within the
    document body so the caller can turn them straight into API ranges.
    """
    styles = []
    out = []
    length = 0
    i = 0

    while i < len(text):
        if text.startswith("**", i):
            close = text.find("**", i + 2)
            if close != -1:
                inner = text[i + 2:close]
                styles.append(("bold", offset + length, offset + length + len(inner)))
                out.append(inner)
                length += len(inner)
                i = close + 2
                continue
        if text[i] == "`":
            close = text.find("`", i + 1)
            if close != -1:
                inner = text[i + 1:close]
                styles.append(("code", offset + length, offset + length + len(inner)))
                out.append(inner)
                length += len(inner)
                i = close + 1
                continue
        out.append(text[i])
        length += 1
        i += 1

    return "".join(out), styles


def markdown_to_docs(markdown_text: str):
    """
    Returns (plain_text, requests).

    plain_text goes in as a single insertText at index 1; requests then style
    it. No styling request changes text length, so every index stays valid
    inside one batchUpdate.
    """
    # Nothing but whitespace means nothing to publish — bail before building requests
    if not markdown_text or not markdown_text.strip():
        return "", []

    plain_parts = []
    offset = 0           # offset within plain text (doc index = offset + 1)
    inline_styles = []   # ("bold"|"code", start, end)
    headings = []        # (start, end, style_name)
    bullet_runs = []     # (start, end) of consecutive list paragraphs
    code_blocks = []     # (start, end)

    current_bullets = None
    in_fence = False

    for raw_line in markdown_text.split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()

        # ``` fence toggles verbatim mode — no markdown parsing inside
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue

        if in_fence:
            start = offset
            plain_parts.append(line + "\n")
            offset += len(line) + 1
            code_blocks.append((start, offset))
            if current_bullets:
                bullet_runs.append(current_bullets)
                current_bullets = None
            continue

        # Horizontal rules carry no meaning in a Doc — drop them
        if HR_RE.match(line):
            continue

        is_list = False
        heading_level = 0
        content = stripped

        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            if 1 <= hashes <= 6 and stripped[hashes:hashes + 1] in (" ", ""):
                heading_level = hashes
                content = stripped[hashes:].strip()
        elif BULLET_RE.match(stripped):
            is_list = True
            content = BULLET_RE.sub("", stripped)
        elif ORDERED_RE.match(stripped):
            is_list = True
            content = ORDERED_RE.sub("", stripped)

        clean, styles = _parse_inline(content, offset)
        inline_styles.extend(styles)

        start = offset
        plain_parts.append(clean + "\n")
        offset += len(clean) + 1

        if heading_level:
            headings.append((start, offset, HEADING_STYLES[heading_level]))

        # Group consecutive list items so each run becomes one list
        if is_list:
            if current_bullets is None:
                current_bullets = (start, offset)
            else:
                current_bullets = (current_bullets[0], offset)
        elif current_bullets:
            bullet_runs.append(current_bullets)
            current_bullets = None

    if current_bullets:
        bullet_runs.append(current_bullets)

    plain_text = "".join(plain_parts)
    if not plain_text:
        return "", []

    requests = [{
        "insertText": {"location": {"index": 1}, "text": plain_text}
    }]

    for start, end, style in headings:
        requests.append({
            "updateParagraphStyle": {
                "range": {"startIndex": start + 1, "endIndex": end + 1},
                "paragraphStyle": {"namedStyleType": style},
                "fields": "namedStyleType",
            }
        })

    for start, end in bullet_runs:
        requests.append({
            "createParagraphBullets": {
                "range": {"startIndex": start + 1, "endIndex": end + 1},
                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
            }
        })

    for start, end in code_blocks:
        requests.append({
            "updateTextStyle": {
                "range": {"startIndex": start + 1, "endIndex": end + 1},
                "textStyle": {"weightedFontFamily": {"fontFamily": "Courier New"}},
                "fields": "weightedFontFamily",
            }
        })

    for kind, start, end in inline_styles:
        if start == end:
            continue
        if kind == "bold":
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": start + 1, "endIndex": end + 1},
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            })
        else:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": start + 1, "endIndex": end + 1},
                    "textStyle": {"weightedFontFamily": {"fontFamily": "Courier New"}},
                    "fields": "weightedFontFamily",
                }
            })

    return plain_text, requests


# ── Confluence ───────────────────────────────────────────────

def markdown_to_storage(markdown_text: str) -> str:
    """
    Convert Markdown to Confluence storage format (XHTML).

    Confluence storage accepts standard XHTML for headings, lists, emphasis
    and pre/code, so the stdlib-adjacent `markdown` package gets us there.
    Falls back to escaped paragraphs if the package is missing, so a publish
    never hard-fails on a missing dependency.
    """
    try:
        import markdown as md
    except ImportError:
        print("  [warn] `markdown` not installed — publishing as plain paragraphs. "
              "Run: python -m pip install markdown")
        paragraphs = [p.strip() for p in markdown_text.split("\n\n") if p.strip()]
        return "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)

    return md.markdown(
        markdown_text,
        extensions=["fenced_code", "tables", "sane_lists"],
    )