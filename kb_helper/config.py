"""Configuration: a YAML file (config.yaml) that the server can both read and write, plus
environment variables (with ${VAR} substitution and ANTHROPIC_API_KEY / KB_HELPER_* overrides)."""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULT_MODEL = "claude-opus-5"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    model: str = DEFAULT_MODEL
    effort: str = "high"
    max_tokens: int = 16000
    fallbacks: bool = True
    max_tool_rounds: int = 12
    extra_instructions: str = ""
    api_key: str | None = None
    api_key_from_env: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    connectors: list[dict[str, Any]] = field(default_factory=list)
    source_path: str | None = None


def expand_env(value: Any) -> Any:
    """Recursively replace ${VAR} / ${VAR:-default} in strings with environment values."""
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"Environment variable {name} is referenced in config but not set")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def resolve_config_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Explicit ``path`` -> $KB_HELPER_CONFIG -> ./config.yaml."""
    return Path(path or os.environ.get("KB_HELPER_CONFIG") or "config.yaml")


def settings_from_dict(raw: dict[str, Any], source_path: str | None = None) -> Settings:
    raw = expand_env(raw or {})
    assistant = raw.get("assistant", {}) or {}
    server = raw.get("server", {}) or {}
    env_key = os.environ.get("ANTHROPIC_API_KEY") or None
    settings = Settings(
        model=os.environ.get("KB_HELPER_MODEL") or assistant.get("model") or DEFAULT_MODEL,
        effort=assistant.get("effort") or "high",
        max_tokens=int(assistant.get("max_tokens") or 16000),
        fallbacks=bool(assistant.get("fallbacks", True)),
        max_tool_rounds=int(assistant.get("max_tool_rounds") or 12),
        extra_instructions=assistant.get("extra_instructions", "") or "",
        api_key=env_key or (assistant.get("api_key") or None),
        api_key_from_env=bool(env_key),
        host=server.get("host", "127.0.0.1"),
        port=int(server.get("port", 8000)),
        connectors=list(raw.get("connectors", []) or []),
        source_path=source_path,
    )
    if settings.effort not in EFFORT_LEVELS:
        raise ConfigError(f"assistant.effort must be one of {', '.join(EFFORT_LEVELS)}, got {settings.effort!r}")
    return settings


class ConfigStore:
    """Owns the raw YAML document so the UI can edit it and the server can persist it."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        load_dotenv()
        self.path = resolve_config_path(path)
        self.data: dict[str, Any] = {}
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            if not isinstance(loaded, dict):
                raise ConfigError(f"{self.path}: top level of the config must be a mapping")
            self.data = loaded
        self.data.setdefault("assistant", {})
        self.data.setdefault("connectors", [])

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def settings(self) -> Settings:
        return settings_from_dict(copy.deepcopy(self.data), str(self.path))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(self.data, sort_keys=False, allow_unicode=True, default_flow_style=False)
        self.path.write_text(text, encoding="utf-8")
        try:
            self.path.chmod(0o600)  # may contain an API key or client secret
        except OSError:
            pass

    # -- assistant section ---------------------------------------------------------------
    def update_assistant(self, **fields: Any) -> None:
        section = self.data.setdefault("assistant", {}) or {}
        for key, value in fields.items():
            if value is None:
                section.pop(key, None)
            else:
                section[key] = value
        self.data["assistant"] = section

    # -- connectors section --------------------------------------------------------------
    def connectors(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in (self.data.get("connectors") or []) if isinstance(entry, dict)]

    def get_connector(self, name: str) -> dict[str, Any] | None:
        return next((dict(e) for e in self.connectors() if e.get("name") == name), None)

    def upsert_connector(self, entry: dict[str, Any], *, previous_name: str | None = None) -> None:
        entries = self.connectors()
        target = previous_name or entry["name"]
        for index, existing in enumerate(entries):
            if existing.get("name") == target:
                entries[index] = entry
                break
        else:
            entries.append(entry)
        self.data["connectors"] = entries

    def remove_connector(self, name: str) -> bool:
        entries = self.connectors()
        remaining = [e for e in entries if e.get("name") != name]
        self.data["connectors"] = remaining
        return len(remaining) != len(entries)


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Convenience for the CLI and tests: read the config file and return Settings."""
    return ConfigStore(path).settings()
