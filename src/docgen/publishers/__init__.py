"""Publishing targets. Each takes a GeneratedDocument and returns the published URL."""
from __future__ import annotations

from typing import Protocol

from docgen.models import GeneratedDocument

TARGETS = ("none", "gdocs", "confluence")


class Publisher(Protocol):
    name: str

    def publish(self, document: GeneratedDocument, *, public: bool = False) -> str: ...


def get_publisher(name: str) -> Publisher:
    # Imported lazily so the Google client libraries only load when needed.
    if name == "gdocs":
        from docgen.publishers.gdocs import GoogleDocsPublisher
        return GoogleDocsPublisher()
    if name == "confluence":
        from docgen.publishers.confluence import ConfluencePublisher
        return ConfluencePublisher()
    raise ValueError(f"unknown publish target {name!r}; choose from {', '.join(TARGETS)}")


__all__ = ["TARGETS", "Publisher", "get_publisher"]
