"""A connector over a local directory. Useful for testing, demos, and as a template for new
connectors. Search is a simple keyword ranking over extracted text."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ..models import Document, SearchHit
from .base import Connector, ConnectorError
from .extract import extract_text, is_supported

_WORD = re.compile(r"[A-Za-z0-9_]+")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text) if len(t) > 1]


class LocalFolderConnector(Connector):
    type_name = "local_folder"
    type_label = "Local folder"
    type_description = "Documents in a folder on the machine running the helper (Markdown, Word, PDF, ...)."

    @classmethod
    def config_fields(cls) -> list[dict[str, Any]]:
        return [
            {"key": "path", "label": "Folder path", "type": "text", "required": True,
             "help": "Absolute path, or relative to where the server runs. Example: ./sample_kb"},
        ]

    def __init__(self, name: str, description: str = "", *, path: str, max_file_bytes: int = 20_000_000) -> None:
        super().__init__(name, description or f"Files under {path}")
        self.root = Path(path).expanduser().resolve()
        if not self.root.is_dir():
            raise ConnectorError(f"local_folder connector {name!r}: {self.root} is not a directory")
        self.max_file_bytes = max_file_bytes
        self._cache: dict[str, tuple[float, str]] = {}

    # -- helpers -----------------------------------------------------------------------------
    def _iter_files(self) -> list[Path]:
        files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for filename in filenames:
                if filename.startswith(".") or not is_supported(filename):
                    continue
                files.append(Path(dirpath) / filename)
        return sorted(files)

    def _text_for(self, file: Path) -> str:
        key = str(file)
        mtime = file.stat().st_mtime
        cached = self._cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        if file.stat().st_size > self.max_file_bytes:
            text = ""
        else:
            text = extract_text(file.read_bytes(), file.name)
        self._cache[key] = (mtime, text)
        return text

    def _resolve(self, document_id: str) -> Path:
        candidate = (self.root / document_id).resolve()
        if self.root not in candidate.parents and candidate != self.root:
            raise ConnectorError(f"{document_id!r} is outside the configured folder")
        if not candidate.is_file():
            raise ConnectorError(f"No file named {document_id!r} in {self.name}")
        return candidate

    @staticmethod
    def _snippet(text: str, terms: list[str], width: int = 240) -> str:
        lowered = text.lower()
        position = -1
        for term in terms:
            position = lowered.find(term)
            if position >= 0:
                break
        if position < 0:
            return text[:width].strip()
        start = max(0, position - width // 3)
        end = min(len(text), start + width)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(text) else ""
        return prefix + text[start:end].replace("\n", " ").strip() + suffix

    # -- Connector API -----------------------------------------------------------------------
    def search(self, query: str, limit: int = 8) -> list[SearchHit]:
        terms = _tokens(query)
        if not terms:
            return []
        scored: list[tuple[float, Path, str]] = []
        for file in self._iter_files():
            text = self._text_for(file)
            if not text:
                continue
            title_tokens = _tokens(file.stem.replace("-", " ").replace("_", " "))
            body = text.lower()
            score = 0.0
            for term in terms:
                title_hits = sum(1 for t in title_tokens if term in t)
                body_hits = body.count(term)
                score += title_hits * 5 + min(body_hits, 20)
            if score > 0:
                scored.append((score, file, text))
        scored.sort(key=lambda item: (-item[0], str(item[1])))
        hits = []
        for score, file, text in scored[:limit]:
            relative = file.relative_to(self.root).as_posix()
            hits.append(
                SearchHit(
                    connector=self.name,
                    document_id=relative,
                    title=file.stem.replace("-", " ").replace("_", " "),
                    snippet=self._snippet(text, terms),
                    url=file.as_uri(),
                    kind="file",
                )
            )
        return hits

    def fetch(self, document_id: str) -> Document:
        file = self._resolve(document_id)
        text = self._text_for(file)
        if not text:
            raise ConnectorError(f"{document_id!r} has no extractable text")
        return Document(
            connector=self.name,
            document_id=document_id,
            title=file.stem.replace("-", " ").replace("_", " "),
            text=text,
            url=file.as_uri(),
            kind="file",
            metadata={"path": str(file), "bytes": file.stat().st_size},
        )

    def health(self) -> dict[str, Any]:
        return {"ok": True, "files": len(self._iter_files()), "path": str(self.root)}
