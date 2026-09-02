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
    app = create_app(assistant=ScriptedAssistant(connectors), config_path=str(kb_dir / "config.yaml"))
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
    assert client.get("/api/connectors").json() == []  # nothing in the config file yet
    health = client.get("/api/health").json()
    assert health["ok"] is True and health["connectors"]["docs"]["ok"] is True
    assert client.post("/api/chat", json={"message": ""}).status_code == 422


def test_bad_connector_config_surfaces_in_health(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    settings = Settings(
        connectors=[{"name": "x", "type": "local_folder", "options": {"path": str(tmp_path / "missing")}}],
        source_path=str(tmp_path / "config.yaml"),
    )
    client = TestClient(create_app(settings=settings))
    health = client.get("/api/health").json()
    assert health["ok"] is False and "not a directory" in health["connector_errors"]["x"]
    listed = client.get("/api/connectors").json()
    assert listed[0]["name"] == "x" and "not a directory" in listed[0]["status"]["error"]


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
    client = TestClient(create_app(assistant=ScriptedAssistant(connectors), config_path=str(kb_dir / "c.yaml")))
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
    client = TestClient(create_app(assistant=Recorder(connectors), config_path=str(kb_dir / "c.yaml")))
    assert client.post("/api/chat", json={"message": "hi", "session_id": "abc123"}).json()["session_id"] == "abc123"
    assert seen == ["abc123"]
    assert client.post("/api/chat", json={"message": "hi", "session_id": "bad id!"}).status_code == 422


def test_chat_without_api_key_returns_setup_error(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = TestClient(create_app(config_path=str(tmp_path / "config.yaml")))
    data = client.post("/api/chat", json={"message": "hi"}).json()
    assert data["kind"] == "error" and data["setup_required"] == "api_key"
    assert "API key" in data["text"]


def test_settings_and_connector_management(tmp_path, kb_dir, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class FakeConfidential:  # keeps the test offline; MSAL would otherwise contact Microsoft
        def __init__(self, *a, **k):
            pass

        def acquire_token_for_client(self, scopes):
            return {"access_token": "t", "expires_in": 3600}

    class FakePublic:
        def __init__(self, *a, **k):
            pass

        def get_accounts(self):
            return []

    import kb_helper.connectors.msgraph_auth as auth_mod
    monkeypatch.setattr(auth_mod.msal, "ConfidentialClientApplication", FakeConfidential)
    monkeypatch.setattr(auth_mod.msal, "PublicClientApplication", FakePublic)
    config = tmp_path / "config.yaml"
    client = TestClient(create_app(config_path=str(config)))

    initial = client.get("/api/settings").json()
    assert initial["api_key_set"] is False and initial["config_exists"] is False
    assert initial["model"] == "claude-opus-5"

    updated = client.put("/api/settings", json={"api_key": "sk-ant-api03-abcdefghijklmnop", "model": "claude-sonnet-5", "effort": "medium", "extra_instructions": "We use Jira."}).json()
    assert updated["api_key_set"] is True and updated["api_key_hint"].startswith("sk-ant-") and "…" in updated["api_key_hint"]
    assert updated["model"] == "claude-sonnet-5" and updated["effort"] == "medium" and updated["config_exists"] is True
    assert client.app.state.state.assistant.model == "claude-sonnet-5"
    assert "We use Jira." in client.app.state.state.assistant.system_prompt
    assert "sk-ant-api03-abcdefghijklmnop" in config.read_text()
    assert client.put("/api/settings", json={"effort": "turbo"}).status_code == 422

    types = {t["type"]: t for t in client.get("/api/connector-types").json()}
    assert {"sharepoint", "local_folder"} <= set(types)
    assert types["local_folder"]["fields"][0]["key"] == "path"
    assert any(f["type"] == "password" for f in types["sharepoint"]["fields"])

    created = client.post("/api/connectors", json={"name": "docs", "type": "local_folder", "description": "Runbooks", "options": {"path": str(kb_dir)}})
    assert created.status_code == 201
    body = created.json()
    assert body["status"]["ok"] is True and body["status"]["files"] >= 3 and body["needs_login"] is False
    assert "docs" in client.app.state.state.assistant.connectors
    assert client.post("/api/connectors", json={"name": "docs", "type": "local_folder", "options": {"path": "."}}).status_code == 409
    assert client.post("/api/connectors", json={"name": "bad", "type": "nope", "options": {}}).status_code == 422
    assert client.post("/api/connectors", json={"name": "bad name!", "type": "local_folder", "options": {}}).status_code == 422

    sp = client.post("/api/connectors", json={
        "name": "sharepoint", "type": "sharepoint", "options": {
            "auth_mode": "client_credentials", "tenant_id": "t", "client_id": "c", "client_secret": "very-secret",
            "sites": ["https://contoso.sharepoint.com/sites/IT"], "search_region": "EMEA"}}).json()
    assert sp["options"]["client_secret"] == "********"
    assert "very-secret" in config.read_text()
    # Saving again with the mask keeps the real secret; the name can change.
    edited = client.put("/api/connectors/sharepoint", json={
        "name": "sharepoint-it", "type": "sharepoint", "description": "IT site", "options": {
            "auth_mode": "client_credentials", "tenant_id": "t", "client_id": "c", "client_secret": "********",
            "sites": ["https://contoso.sharepoint.com/sites/IT"]}}).json()
    assert edited["name"] == "sharepoint-it" and "very-secret" in config.read_text()
    assert edited["status"] == {"ok": True, "auth_mode": "client_credentials", "sites": ["https://contoso.sharepoint.com/sites/IT"], "search_api": None}
    names = [c["name"] for c in client.get("/api/connectors").json()]
    assert names == ["docs", "sharepoint-it"]

    user_sp = client.post("/api/connectors", json={"name": "my-sp", "type": "sharepoint", "options": {"auth_mode": "user"}}).json()
    assert user_sp["needs_login"] is True and user_sp["status"]["signed_in"] is False
    assert client.get("/api/auth", params={"session_id": "s"}).json()["connectors"]["my-sp"]["state"] == "signed_out"

    disabled = client.put("/api/connectors/docs", json={"name": "docs", "type": "local_folder", "enabled": False, "options": {"path": str(kb_dir)}}).json()
    assert disabled["enabled"] is False and disabled["status"]["error"] == "disabled"
    assert "docs" not in client.app.state.state.assistant.connectors

    assert client.delete("/api/connectors/docs").json() == {"deleted": True}
    assert client.delete("/api/connectors/docs").status_code == 404
    assert client.post("/api/connectors/my-sp/test").json()["signed_in"] is False

    # A fresh app built from the saved file sees the same configuration.
    again = TestClient(create_app(config_path=str(config)))
    assert [c["name"] for c in again.get("/api/connectors").json()] == ["sharepoint-it", "my-sp"]
    assert again.get("/api/settings").json()["model"] == "claude-sonnet-5"


def test_unhandled_errors_are_json(kb_dir):
    class Exploding(ScriptedAssistant):
        def respond(self, history, user_message, on_event=None):
            raise RuntimeError("boom")

    connectors = {"docs": LocalFolderConnector("docs", "Docs", path=str(kb_dir))}
    client = TestClient(create_app(assistant=Exploding(connectors), config_path=str(kb_dir / "c.yaml")), raise_server_exceptions=False)
    res = client.post("/api/chat", json={"message": "hi"})
    assert res.status_code == 500 and res.json()["detail"] == "RuntimeError: boom"
