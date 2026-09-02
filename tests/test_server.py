from __future__ import annotations

from fastapi.testclient import TestClient

from kb_helper.agent import Assistant, Turn
from kb_helper.config import Settings
from kb_helper.connectors.local_folder import LocalFolderConnector
from kb_helper.models import Source
from kb_helper.server import create_app


class ScriptedAssistant(Assistant):
    def __init__(self, connectors):
        super().__init__(connectors, client=object())
        self.calls = []

    def respond(self, history, user_message, on_event=None):
        self.calls.append(user_message)
        history.append({"role": "user", "content": user_message})
        if len(self.calls) == 1:
            return Turn(kind="question", text="Which env?", options=["staging", "prod"])
        return Turn(kind="answer", text=f"Answer to {user_message!r}", sources=[Source("docs", "a.md", "A", "file:///a.md")], events=[{"type": "search", "query": "x", "connector": None}])


def make_client(kb_dir):
    connectors = {"docs": LocalFolderConnector("docs", "Docs", path=str(kb_dir))}
    app = create_app(assistant=ScriptedAssistant(connectors), settings=Settings())
    return TestClient(app)


def test_chat_flow_and_session_persistence(kb_dir):
    client = make_client(kb_dir)
    first = client.post("/api/chat", json={"message": "deploy?"}).json()
    assert first["kind"] == "question" and first["options"] == ["staging", "prod"]
    session_id = first["session_id"]

    second = client.post("/api/chat", json={"message": "prod", "session_id": session_id}).json()
    assert second["session_id"] == session_id
    assert second["kind"] == "answer" and second["sources"][0]["title"] == "A"

    transcript = client.get(f"/api/sessions/{session_id}").json()["transcript"]
    assert [m["role"] for m in transcript] == ["user", "assistant", "user", "assistant"]

    assert client.post(f"/api/sessions/{session_id}/reset").json()["reset"] is True
    assert client.get(f"/api/sessions/{session_id}").json()["transcript"] == []


def test_misc_endpoints(kb_dir):
    client = make_client(kb_dir)
    assert client.get("/").status_code == 200 and "KB Helper" in client.get("/").text
    assert client.get("/api/connectors").json() == [{"name": "docs", "type": "local_folder", "description": "Docs"}]
    health = client.get("/api/health").json()
    assert health["ok"] is True and health["connectors"]["docs"]["ok"] is True
    assert client.post("/api/chat", json={"message": ""}).status_code == 422


def test_bad_connector_config_surfaces_in_health(tmp_path):
    settings = Settings(connectors=[{"name": "x", "type": "local_folder", "options": {"path": str(tmp_path / "missing")}}])
    client = TestClient(create_app(settings=settings))
    health = client.get("/api/health").json()
    assert health["ok"] is False and "not a directory" in health["error"]
    assert client.get("/api/connectors").json() == []


class FakeProvider:
    def __init__(self):
        self.interactive = True
        self.signed = set()
        self.started = []

    def start_login(self, user_key):
        self.started.append(user_key)
        return {"state": "pending", "user_code": "CODE", "verification_uri": "https://x", "message": "m"}

    def status(self, user_key=None):
        return {"mode": "user", "state": "signed_in" if user_key in self.signed else "signed_out", "signed_in": user_key in self.signed}

    def sign_out(self, user_key=None):
        self.signed.discard(user_key)


def test_auth_endpoints(kb_dir):
    from kb_helper.connectors import Connector

    provider = FakeProvider()

    class LoginConnector(LocalFolderConnector):
        def login_provider(self):
            return provider

    connectors = {"sharepoint": LoginConnector("sharepoint", path=str(kb_dir))}
    client = TestClient(create_app(assistant=ScriptedAssistant(connectors), settings=Settings()))
    assert provider.interactive is False  # the web app must never block on the terminal

    overview = client.get("/api/auth", params={"session_id": "s1"}).json()
    assert overview["connectors"]["sharepoint"]["signed_in"] is False
    start = client.post("/api/auth/sharepoint/start", params={"session_id": "s1"}).json()
    assert start["user_code"] == "CODE" and provider.started == ["s1"]
    provider.signed.add("s1")
    assert client.get("/api/auth/sharepoint/status", params={"session_id": "s1"}).json()["state"] == "signed_in"
    assert client.post("/api/auth/sharepoint/signout", params={"session_id": "s1"}).json() == {"signed_in": False}
    assert client.post("/api/auth/nope/start", params={"session_id": "s1"}).status_code == 404


def test_chat_sets_current_user_context(kb_dir):
    from kb_helper.context import current_user

    seen = []

    class Recorder(ScriptedAssistant):
        def respond(self, history, user_message, on_event=None):
            seen.append(current_user.get())
            return Turn(kind="answer", text="ok")

    connectors = {"docs": LocalFolderConnector("docs", "Docs", path=str(kb_dir))}
    client = TestClient(create_app(assistant=Recorder(connectors), settings=Settings()))
    assert client.post("/api/chat", json={"message": "hi", "session_id": "abc123"}).json()["session_id"] == "abc123"
    assert seen == ["abc123"]
    assert client.post("/api/chat", json={"message": "hi", "session_id": "bad id!"}).status_code == 422
