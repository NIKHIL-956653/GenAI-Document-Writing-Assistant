"""
Ticket images in generated documents.

The model is told which images exist and places them with
`![caption](attachment:FILENAME)`. `resolve_images()` then:

- drops references to images that don't exist (the model invented a file),
- moves every reference onto its own line, so publishers can embed it as a block,
- appends images the model didn't place under an "Attachments" heading,
  so no ticket image is silently lost.

Publishers turn the references into embedded images; `localize()` points
them at copies next to the saved Markdown file.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from docgen.models import Attachment, Ticket

log = logging.getLogger(__name__)

IMAGE_REF_RE = re.compile(r"!\[([^\]\n]*)\]\(attachment:([^)\s]+)\)")
IMAGE_LINE_RE = re.compile(r"^\s*!\[([^\]\n]*)\]\(attachment:([^)\s]+)\)\s*$")


def image_name(attachment: Attachment) -> str:
    """
    The name the model, the Markdown and the publishers all use for an image:
    no directories, no spaces (they'd break `attachment:` references and links).
    """
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(attachment.filename).name).strip("._")
    return name or f"image-{attachment.id}"


def resolve_images(body: str, ticket: Ticket, heading_level: int = 2) -> tuple[str, list[Attachment]]:
    available: dict[str, Attachment] = {}
    for attachment in ticket.images:
        available.setdefault(image_name(attachment), attachment)
    placed: list[str] = []

    def place(match: re.Match) -> str:
        caption, name = match.group(1), match.group(2)
        if name not in available:
            log.warning("dropping reference to unknown image", extra={"ticket": ticket.key, "image": name})
            return caption
        if name not in placed:
            placed.append(name)
        return f"\n\n![{caption}](attachment:{name})\n\n"

    # Spaces around an inline reference go with it when it moves onto its own line.
    body = re.sub(r"[ \t]*" + IMAGE_REF_RE.pattern + r"[ \t]*", place, body)
    leftover = [name for name in available if name not in placed]
    if leftover:
        body = body.rstrip() + f"\n\n{'#' * heading_level} Attachments\n\n" + "\n\n".join(
            f"![{name}](attachment:{name})" for name in leftover) + "\n"
    body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
    return body, [available[name] for name in [*placed, *leftover]]


def localize(body: str, images: list[Attachment], folder: Path, relative_to: Path) -> str:
    """Copy images into `folder` and point `attachment:` references at the copies."""
    if not images:
        return body
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for attachment in images:
        if attachment.local_path is None:
            continue
        target = folder / image_name(attachment)
        shutil.copyfile(attachment.local_path, target)
        paths[image_name(attachment)] = target.relative_to(relative_to).as_posix()
    return IMAGE_REF_RE.sub(lambda m: f"![{m.group(1)}]({paths.get(m.group(2), m.group(2))})", body)
