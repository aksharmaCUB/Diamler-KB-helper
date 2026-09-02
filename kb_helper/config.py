"""Configuration loading: YAML file + environment variables (with ${VAR} substitution)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULT_MODEL = "claude-opus-5"
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


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Load settings from a YAML file.

    Resolution order for the file: explicit ``path`` -> $KB_HELPER_CONFIG -> ./config.yaml.
    A missing file yields default settings with no connectors (the server still starts so the
    health endpoint can report the problem).
    """
    load_dotenv()
    candidate = path or os.environ.get("KB_HELPER_CONFIG") or "config.yaml"
    file = Path(candidate)
    raw: dict[str, Any] = {}
    if file.exists():
        with file.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{file}: top level of the config must be a mapping")
    raw = expand_env(raw)

    assistant = raw.get("assistant", {}) or {}
    server = raw.get("server", {}) or {}
    settings = Settings(
        model=os.environ.get("KB_HELPER_MODEL") or assistant.get("model", DEFAULT_MODEL),
        effort=assistant.get("effort", "high"),
        max_tokens=int(assistant.get("max_tokens", 16000)),
        fallbacks=bool(assistant.get("fallbacks", True)),
        max_tool_rounds=int(assistant.get("max_tool_rounds", 12)),
        extra_instructions=assistant.get("extra_instructions", "") or "",
        host=server.get("host", "127.0.0.1"),
        port=int(server.get("port", 8000)),
        connectors=list(raw.get("connectors", []) or []),
        source_path=str(file) if file.exists() else None,
    )
    if settings.effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ConfigError(f"assistant.effort must be one of low/medium/high/xhigh/max, got {settings.effort!r}")
    return settings
