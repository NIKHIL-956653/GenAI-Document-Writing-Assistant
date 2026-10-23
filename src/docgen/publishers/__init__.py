"""Publishing targets. Each creates or updates a document and says where it is."""
from __future__ import annotations

from typing import Protocol

from docgen.models import GeneratedDocument, Published

TARGETS = ("none", "gdocs", "confluence")


class Publisher(Protocol):
    name: str

    def publish(self, document: GeneratedDocument, *, public: bool = False,
                existing_id: str | None = None) -> Published: ...


def get_publisher(name: str, *, interactive: bool = True) -> Publisher:
    """`interactive=False` (the server) never opens a browser for Google consent."""
    # Imported lazily so the Google client libraries only load when needed.
    if name == "gdocs":
        from docgen.publishers.gdocs import GoogleDocsPublisher
        return GoogleDocsPublisher(interactive=interactive)
    if name == "confluence":
        from docgen.publishers.confluence import ConfluencePublisher
        return ConfluencePublisher()
    raise ValueError(f"unknown publish target {name!r}; choose from {', '.join(TARGETS)}")


__all__ = ["TARGETS", "Publisher", "get_publisher"]
