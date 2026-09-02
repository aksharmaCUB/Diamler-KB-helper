"""Shared data types passed between connectors, the agent and the API."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchHit:
    """One result returned by a connector search."""

    connector: str
    document_id: str
    title: str
    snippet: str = ""
    url: str | None = None
    kind: str = "document"  # file | page | list_item | document
    modified: str | None = None
    author: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "document_id": self.document_id,
            "title": self.title,
            "snippet": self.snippet,
            "url": self.url,
            "kind": self.kind,
            "modified": self.modified,
            "author": self.author,
        }


@dataclass
class Document:
    """Full text of a knowledge-base item fetched by a connector."""

    connector: str
    document_id: str
    title: str
    text: str
    url: str | None = None
    kind: str = "document"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Source:
    """A document the assistant looked at while answering; shown to the user as a citation."""

    connector: str
    document_id: str
    title: str
    url: str | None = None

    def key(self) -> tuple[str, str]:
        return (self.connector, self.document_id)

    def to_dict(self) -> dict[str, Any]:
        return {"connector": self.connector, "document_id": self.document_id, "title": self.title, "url": self.url}
