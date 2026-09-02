from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from ..models import Document, SearchHit


class ConnectorError(Exception):
    """Raised for backend failures the assistant should report rather than crash on."""


class Connector(ABC):
    """Base class for every knowledge source.

    Subclasses set ``type_name`` (the value used under ``type:`` in config.yaml), accept their
    options as keyword arguments in ``__init__`` and implement ``search`` and ``fetch``.
    """

    type_name: ClassVar[str] = "base"
    type_label: ClassVar[str] = "Connector"
    type_description: ClassVar[str] = ""

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description

    @abstractmethod
    def search(self, query: str, limit: int = 8) -> list[SearchHit]:
        """Return up to ``limit`` hits ranked by relevance."""

    @abstractmethod
    def fetch(self, document_id: str) -> Document:
        """Return the full text of a document previously returned by ``search``."""

    def health(self) -> dict[str, Any]:
        """Cheap liveness check used by the /api/health endpoint. Override when meaningful."""
        return {"ok": True}

    @classmethod
    def config_fields(cls) -> list[dict[str, Any]]:
        """Describe the ``options`` this connector accepts so the web UI can render a form.

        Each field: ``{"key", "label", "type": text|password|textarea|select|list|bool,
        "required", "default", "help", "choices": [...], "show_if": {"key": value}}``.
        ``list`` fields are entered one item per line and passed as a list.
        """
        return []

    @classmethod
    def secret_keys(cls) -> set[str]:
        return {f["key"] for f in cls.config_fields() if f.get("type") == "password"}

    def login_provider(self) -> Any | None:
        """Return an object with start_login/status/sign_out when users must sign in themselves
        (see msgraph_auth.UserLoginAuth); None for connectors that need no per-user login."""
        return None

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type_name, "description": self.description}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.__class__.__name__} name={self.name!r}>"
