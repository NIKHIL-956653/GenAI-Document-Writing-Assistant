"""Atlassian Document Format → plain text."""
from __future__ import annotations


def adf_to_text(node) -> str:
    """
    Jira Cloud REST v3 returns rich text as Atlassian Document Format (a JSON tree),
    not a string. Walk it and keep the text so the LLM sees prose, not a Python dict.
    Handles paragraphs, headings, bullet/ordered lists, code blocks, hard breaks.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    children = "".join(adf_to_text(c) for c in node.get("content", []) or [])
    if t in ("paragraph", "heading"):
        return children + "\n"
    if t == "listItem":
        return "- " + children
    if t in ("bulletList", "orderedList", "blockquote"):
        return children + "\n"
    if t == "codeBlock":
        return "```\n" + children + "\n```\n"
    return children
