"""FastAPI app: chat API, settings + connector management (persisted to config.yaml), the
per-user sign-in flow, and the bundled web UI."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .agent import Assistant, Turn
from .config import DEFAULT_MODEL, EFFORT_LEVELS, ConfigError, ConfigStore, Settings
from .connectors import ConnectorError, build_connectors_lenient, builtin_types, resolve_connector_type
from .context import current_user

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
SESSION_TTL_SECONDS = 6 * 3600
SECRET_MASK = "********"
KNOWN_MODELS = [
    {"id": "claude-opus-5", "label": "Claude Opus 5 (recommended)"},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5 (faster, cheaper)"},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5 (cheapest)"},
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8"},
]
_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$"
UPLOADS_CONNECTOR = "uploads"
MAX_UPLOAD_BYTES = 50_000_000


class SessionStore:
    """In-memory conversation histories keyed by session id."""

    def __init__(self, ttl: float = SESSION_TTL_SECONDS) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl

    def get_or_create(self, session_id: str | None) -> tuple[str, dict[str, Any]]:
        with self._lock:
            self._expire()
            if session_id and session_id in self._sessions:
                session = self._sessions[session_id]
            else:
                session_id = session_id or uuid.uuid4().hex
                session = {"history": [], "transcript": [], "lock": threading.Lock()}
                self._sessions[session_id] = session
            session["touched"] = time.time()
            return session_id, session

    def reset(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def _expire(self) -> None:
        cutoff = time.time() - self._ttl
        for key in [k for k, v in self._sessions.items() if v.get("touched", 0) < cutoff]:
            del self._sessions[key]


class AppState:
    """Everything that is rebuilt when the configuration changes."""

    def __init__(self, store: ConfigStore, assistant: Assistant | None = None) -> None:
        self.store = store
        self.lock = threading.RLock()
        self.injected_assistant = assistant
        self.settings: Settings = Settings()
        self.assistant: Assistant = assistant or Assistant({}, api_key=None)
        self.connector_errors: dict[str, str] = {}
        self.login_providers: dict[str, Any] = {}
        self.config_error: str | None = None
        self.rebuild()

    def rebuild(self) -> None:
        with self.lock:
            try:
                self.settings = self.store.settings()
                self.config_error = None
            except ConfigError as exc:
                self.config_error = str(exc)
                log.error("Configuration error: %s", exc)
                self.settings = Settings(connectors=[])
            if self.injected_assistant is not None:
                self.assistant = self.injected_assistant
                self.connector_errors = {}
            else:
                connectors, self.connector_errors = build_connectors_lenient(self.settings.connectors)
                for name, error in self.connector_errors.items():
                    log.error("Connector %s could not be loaded: %s", name, error)
                self.assistant = Assistant(
                    connectors,
                    api_key=self.settings.api_key,
                    model=self.settings.model,
                    effort=self.settings.effort,
                    max_tokens=self.settings.max_tokens,
                    fallbacks=self.settings.fallbacks,
                    extra_instructions=self.settings.extra_instructions,
                    max_tool_rounds=self.settings.max_tool_rounds,
                )
            # In the web app users sign in through the UI, never through the server's terminal.
            self.login_providers = {}
            for name, connector in self.assistant.connectors.items():
                provider = connector.login_provider()
                if provider is not None:
                    provider.interactive = False
                    self.login_providers[name] = provider


# ---------------------------------------------------------------------------- request models
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]*$")
    connector: str | None = Field(default=None, max_length=40)  # limit this question to one source


class SettingsUpdate(BaseModel):
    api_key: str | None = None  # "" clears the stored key
    model: str | None = Field(default=None, min_length=1, max_length=80)
    effort: str | None = None
    extra_instructions: str | None = Field(default=None, max_length=20_000)


class ConnectorInput(BaseModel):
    name: str = Field(pattern=_NAME_PATTERN)
    type: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


def create_app(
    assistant: Assistant | None = None,
    settings: Settings | None = None,
    config_path: str | None = None,
) -> FastAPI:
    """``assistant``/``settings`` are for tests; normally everything comes from the config file."""
    store = ConfigStore(config_path or (settings.source_path if settings else None))
    if settings is not None and assistant is None:
        store.data["connectors"] = list(settings.connectors)
    state = AppState(store, assistant)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info(
            "KB helper ready: config=%s model=%s connectors=%s",
            store.path, state.assistant.model, list(state.assistant.connectors),
        )
        yield

    app = FastAPI(title="KB Helper", lifespan=lifespan)
    app.state.state = state
    app.state.sessions = SessionStore()

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error: %s", exc)
        return JSONResponse(status_code=500, content={"detail": f"{exc.__class__.__name__}: {exc}"})

    # ------------------------------------------------------------------ helpers
    def connector_types() -> list[dict[str, Any]]:
        return [
            {"type": t, "label": cls.type_label, "description": cls.type_description, "fields": cls.config_fields()}
            for t, cls in builtin_types().items()
        ]

    def masked_options(entry: dict[str, Any]) -> dict[str, Any]:
        options = dict(entry.get("options") or {})
        try:
            secrets = resolve_connector_type(str(entry.get("type"))).secret_keys()
        except ConnectorError:
            secrets = set()
        return {k: (SECRET_MASK if k in secrets and v else v) for k, v in options.items()}

    def connector_status(name: str, session_id: str | None) -> dict[str, Any]:
        if name in state.connector_errors:
            return {"ok": False, "error": state.connector_errors[name]}
        connector = state.assistant.connectors.get(name)
        if connector is None:
            return {"ok": False, "error": "disabled"}
        try:
            provider = state.login_providers.get(name)
            if provider is not None:
                info = {"ok": True, **provider.status(session_id or current_user.get())}
            else:
                info = connector.health()
        except Exception as exc:  # noqa: BLE001
            info = {"ok": False, "error": str(exc)}
        return info

    def connector_view(entry: dict[str, Any], session_id: str | None) -> dict[str, Any]:
        name = str(entry.get("name"))
        enabled = bool(entry.get("enabled", True))
        try:
            label = resolve_connector_type(str(entry.get("type"))).type_label
        except ConnectorError:
            label = str(entry.get("type"))
        return {
            "name": name,
            "type": entry.get("type"),
            "type_label": label,
            "description": entry.get("description", "") or "",
            "enabled": enabled,
            "options": masked_options(entry),
            "needs_login": name in state.login_providers,
            "status": connector_status(name, session_id) if enabled else {"ok": False, "error": "disabled"},
        }

    def settings_view() -> dict[str, Any]:
        s = state.settings
        key = s.api_key or ""
        return {
            "config_path": str(store.path),
            "config_exists": store.exists,
            "config_error": state.config_error,
            "model": s.model,
            "effort": s.effort,
            "max_tokens": s.max_tokens,
            "extra_instructions": s.extra_instructions,
            "api_key_set": bool(key),
            "api_key_hint": (key[:7] + "…" + key[-4:]) if len(key) > 12 else ("set" if key else ""),
            "api_key_from_env": s.api_key_from_env,
            "models": KNOWN_MODELS,
            "efforts": list(EFFORT_LEVELS),
        }

    def _provider(name: str) -> Any:
        provider = state.login_providers.get(name)
        if provider is None:
            raise HTTPException(status_code=404, detail=f"No sign-in available for connector {name!r}")
        return provider

    def _save_and_rebuild() -> None:
        try:
            store.save()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not write {store.path}: {exc}") from exc
        state.rebuild()

    # ------------------------------------------------------------------ static
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    # ------------------------------------------------------------------ health / settings
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": state.config_error is None and not state.connector_errors,
            "model": state.assistant.model,
            "api_key_set": bool(state.settings.api_key),
            "error": state.config_error,
            "connectors": {name: connector_status(name, None) for name in state.assistant.connectors},
            "connector_errors": state.connector_errors,
        }

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return settings_view()

    @app.put("/api/settings")
    def update_settings(update: SettingsUpdate) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if update.api_key is not None:
            fields["api_key"] = update.api_key.strip() or None
        if update.model is not None:
            fields["model"] = update.model.strip() or DEFAULT_MODEL
        if update.effort is not None:
            if update.effort not in EFFORT_LEVELS:
                raise HTTPException(status_code=422, detail=f"effort must be one of {', '.join(EFFORT_LEVELS)}")
            fields["effort"] = update.effort
        if update.extra_instructions is not None:
            fields["extra_instructions"] = update.extra_instructions.strip() or None
        with state.lock:
            store.update_assistant(**fields)
            _save_and_rebuild()
        return settings_view()

    # ------------------------------------------------------------------ connectors
    @app.get("/api/connector-types")
    def list_connector_types() -> list[dict[str, Any]]:
        return connector_types()

    @app.get("/api/connectors")
    def list_connectors(session_id: str | None = None) -> list[dict[str, Any]]:
        return [connector_view(entry, session_id) for entry in store.connectors()]

    def _apply_connector(body: ConnectorInput, previous: dict[str, Any] | None) -> dict[str, Any]:
        try:
            cls = resolve_connector_type(body.type)
        except ConnectorError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        options: dict[str, Any] = {}
        previous_options = (previous or {}).get("options") or {}
        for key, value in body.options.items():
            if key in cls.secret_keys() and (value in (None, "", SECRET_MASK)):
                value = previous_options.get(key)
            if value in (None, "", []):
                continue
            options[key] = value
        entry = {
            "name": body.name.strip(),
            "type": body.type,
            "description": body.description.strip(),
            "enabled": body.enabled,
            "options": options,
        }
        clash = store.get_connector(entry["name"])
        if clash is not None and (previous is None or clash.get("name") != previous.get("name")):
            raise HTTPException(status_code=409, detail=f"A connector named {entry['name']!r} already exists")
        with state.lock:
            store.upsert_connector(entry, previous_name=(previous or {}).get("name"))
            _save_and_rebuild()
        return connector_view(entry, None)

    @app.post("/api/connectors", status_code=201)
    def add_connector(body: ConnectorInput) -> dict[str, Any]:
        return _apply_connector(body, None)

    @app.put("/api/connectors/{name}")
    def update_connector(name: str, body: ConnectorInput) -> dict[str, Any]:
        previous = store.get_connector(name)
        if previous is None:
            raise HTTPException(status_code=404, detail=f"No connector named {name!r}")
        return _apply_connector(body, previous)

    @app.delete("/api/connectors/{name}")
    def delete_connector(name: str) -> dict[str, Any]:
        with state.lock:
            if not store.remove_connector(name):
                raise HTTPException(status_code=404, detail=f"No connector named {name!r}")
            _save_and_rebuild()
        return {"deleted": True}

    @app.post("/api/connectors/{name}/test")
    def test_connector(name: str, session_id: str | None = None) -> dict[str, Any]:
        if store.get_connector(name) is None:
            raise HTTPException(status_code=404, detail=f"No connector named {name!r}")
        return connector_status(name, session_id)

    # ------------------------------------------------------------------ uploads
    def uploads_dir() -> Path:
        return store.path.resolve().parent / "uploads"

    @app.post("/api/uploads", status_code=201)
    def upload_files(files: list[UploadFile] = File(...)) -> dict[str, Any]:
        """Store files next to the config and expose them through a local_folder connector
        named 'uploads' (created on first use)."""
        from .connectors.extract import is_supported

        target = uploads_dir()
        target.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        skipped: list[str] = []
        for upload in files:
            name = Path(upload.filename or "").name
            if not name or name.startswith("."):
                continue
            if not is_supported(name):
                skipped.append(name)
                continue
            data = upload.file.read(MAX_UPLOAD_BYTES + 1)
            if len(data) > MAX_UPLOAD_BYTES:
                skipped.append(f"{name} (over {MAX_UPLOAD_BYTES // 1_000_000} MB)")
                continue
            (target / name).write_bytes(data)
            saved.append(name)
        if saved and store.get_connector(UPLOADS_CONNECTOR) is None:
            with state.lock:
                store.upsert_connector({
                    "name": UPLOADS_CONNECTOR,
                    "type": "local_folder",
                    "description": "Files uploaded through the chat UI",
                    "enabled": True,
                    "options": {"path": str(target)},
                })
                _save_and_rebuild()
        else:
            state.rebuild()  # refresh file counts
        return {"saved": saved, "skipped": skipped, "connector": UPLOADS_CONNECTOR, "path": str(target)}

    # ------------------------------------------------------------------ chat
    @app.post("/api/chat")
    def chat(request: ChatRequest) -> JSONResponse:
        session_id, session = app.state.sessions.get_or_create(request.session_id)
        if not state.settings.api_key and state.injected_assistant is None:
            turn = Turn(kind="error", text="No Anthropic API key is configured yet. Open Settings and add one.")
            return JSONResponse({"session_id": session_id, "setup_required": "api_key", **turn.to_dict()})
        only: list[str] | None = None
        if request.connector:
            if request.connector not in state.assistant.connectors:
                raise HTTPException(status_code=422, detail=f"Unknown or disabled connector {request.connector!r}")
            only = [request.connector]
        if not session["lock"].acquire(blocking=False):
            raise HTTPException(status_code=409, detail="This session is still processing a previous message.")
        token = current_user.set(session_id)
        try:
            turn = state.assistant.respond(session["history"], request.message, only_connectors=only)
            session["transcript"].append({"role": "user", "text": request.message})
            session["transcript"].append({"role": "assistant", **turn.to_dict()})
        finally:
            current_user.reset(token)
            session["lock"].release()
        return JSONResponse({"session_id": session_id, **turn.to_dict()})

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        _, session = app.state.sessions.get_or_create(session_id)
        return {"session_id": session_id, "transcript": session["transcript"]}

    @app.post("/api/sessions/{session_id}/reset")
    def reset_session(session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "reset": app.state.sessions.reset(session_id)}

    # ------------------------------------------------------------------ per-user sign-in
    @app.get("/api/auth")
    def auth_overview(session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "connectors": {name: provider.status(session_id) for name, provider in state.login_providers.items()},
        }

    @app.post("/api/auth/{connector}/start")
    def auth_start(connector: str, session_id: str) -> dict[str, Any]:
        app.state.sessions.get_or_create(session_id)
        try:
            return _provider(connector).start_login(session_id)
        except ConnectorError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/auth/{connector}/status")
    def auth_status(connector: str, session_id: str) -> dict[str, Any]:
        return _provider(connector).status(session_id)

    @app.post("/api/auth/{connector}/signout")
    def auth_signout(connector: str, session_id: str) -> dict[str, Any]:
        _provider(connector).sign_out(session_id)
        return {"signed_in": False}

    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the KB helper web server")
    parser.add_argument("--config", help="Path to config.yaml (default: $KB_HELPER_CONFIG or ./config.yaml)")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(config_path=args.config)
    settings = app.state.state.settings
    host, port = args.host or settings.host, args.port or settings.port
    log.info("Open http://%s:%d in your browser", host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
