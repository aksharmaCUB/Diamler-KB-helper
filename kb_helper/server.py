"""FastAPI app: JSON chat API + the bundled web UI."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .agent import Assistant, Turn
from .config import Settings, load_settings
from .connectors import ConnectorError, build_connectors
from .context import current_user

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
SESSION_TTL_SECONDS = 6 * 3600


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


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]*$")


def create_app(assistant: Assistant | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    startup_error: str | None = None
    if assistant is None:
        try:
            connectors = build_connectors(settings.connectors)
        except ConnectorError as exc:
            log.error("Connector configuration error: %s", exc)
            connectors = {}
            startup_error = str(exc)
        assistant = Assistant(
            connectors,
            model=settings.model,
            effort=settings.effort,
            max_tokens=settings.max_tokens,
            fallbacks=settings.fallbacks,
            extra_instructions=settings.extra_instructions,
            max_tool_rounds=settings.max_tool_rounds,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info("KB helper ready: model=%s connectors=%s", assistant.model, list(assistant.connectors))
        yield

    # In the web app users sign in through the UI, never through the server's terminal.
    login_providers: dict[str, Any] = {}
    for name, connector in assistant.connectors.items():
        provider = connector.login_provider()
        if provider is not None:
            provider.interactive = False
            login_providers[name] = provider

    app = FastAPI(title="KB Helper", lifespan=lifespan)
    app.state.assistant = assistant
    app.state.sessions = SessionStore()
    app.state.startup_error = startup_error
    app.state.login_providers = login_providers

    def _provider(name: str) -> Any:
        provider = login_providers.get(name)
        if provider is None:
            raise HTTPException(status_code=404, detail=f"No sign-in available for connector {name!r}")
        return provider

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        connectors: dict[str, Any] = {}
        for name, connector in assistant.connectors.items():
            try:
                connectors[name] = connector.health()
            except Exception as exc:  # noqa: BLE001
                connectors[name] = {"ok": False, "error": str(exc)}
        return {"ok": startup_error is None, "model": assistant.model, "error": startup_error, "connectors": connectors}

    @app.get("/api/connectors")
    def list_connectors() -> list[dict[str, Any]]:
        return [c.describe() for c in assistant.connectors.values()]

    @app.post("/api/chat")
    def chat(request: ChatRequest) -> JSONResponse:
        session_id, session = app.state.sessions.get_or_create(request.session_id)
        if not session["lock"].acquire(blocking=False):
            raise HTTPException(status_code=409, detail="This session is still processing a previous message.")
        token = current_user.set(session_id)
        try:
            turn: Turn = assistant.respond(session["history"], request.message)
            session["transcript"].append({"role": "user", "text": request.message})
            session["transcript"].append({"role": "assistant", **turn.to_dict()})
        finally:
            current_user.reset(token)
            session["lock"].release()
        return JSONResponse({"session_id": session_id, **turn.to_dict()})

    # ---- per-user sign-in (device code flow) -------------------------------------------
    @app.get("/api/auth")
    def auth_overview(session_id: str) -> dict[str, Any]:
        """Which connectors need a personal sign-in, and whether this session has one."""
        return {
            "session_id": session_id,
            "connectors": {name: provider.status(session_id) for name, provider in login_providers.items()},
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

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        _, session = app.state.sessions.get_or_create(session_id)
        return {"session_id": session_id, "transcript": session["transcript"]}

    @app.post("/api/sessions/{session_id}/reset")
    def reset_session(session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "reset": app.state.sessions.reset(session_id)}

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
    settings = load_settings(args.config)
    app = create_app(settings=settings)
    uvicorn.run(app, host=args.host or settings.host, port=args.port or settings.port)


if __name__ == "__main__":
    main()
