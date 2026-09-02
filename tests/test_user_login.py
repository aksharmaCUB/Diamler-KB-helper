"""UserLoginAuth against a fake MSAL PublicClientApplication (no network)."""
from __future__ import annotations

import threading
import time

import pytest

from kb_helper.connectors import ConnectorError
from kb_helper.connectors.msgraph_auth import DEFAULT_PUBLIC_CLIENT_ID, AuthRequired, UserLoginAuth
from kb_helper.context import current_user


class FakeMsalApp:
    """Mimics the parts of msal.PublicClientApplication we use, with a shared 'directory'
    so a fresh instance built from a persisted cache sees the same accounts."""

    directory: dict[str, list[dict]] = {}
    release = threading.Event()

    def __init__(self, client_id, authority=None, token_cache=None):
        self.client_id = client_id
        self.authority = authority
        self.cache = token_cache
        self.calls = []

    def _accounts(self):
        # Accounts are "persisted" by writing to the cache's serialize() payload.
        return FakeMsalApp.directory.setdefault(id(self.cache), [])

    def get_accounts(self):
        # Pretend deserialisation restored accounts by reading the cache blob.
        blob = self.cache.serialize() if self.cache else ""
        if blob and "user@contoso.com" in blob:
            return [{"username": "user@contoso.com"}]
        return list(self._accounts())

    def acquire_token_silent(self, scopes, account=None):
        self.calls.append("silent")
        return {"access_token": "silent-token", "expires_in": 3600}

    def initiate_device_flow(self, scopes):
        self.calls.append("device_flow")
        return {"user_code": "ABCD1234", "verification_uri": "https://microsoft.com/devicelogin", "message": "go", "expires_in": 900}

    def acquire_token_by_device_flow(self, flow):
        self.calls.append("wait")
        FakeMsalApp.release.wait(timeout=5)
        self._accounts().append({"username": "user@contoso.com"})
        if self.cache is not None:
            self.cache.add({"response": {}, "environment": "x"})  # marks state changed
            self.cache._signed = True
        return {"access_token": "device-token", "expires_in": 3600}

    def remove_account(self, account):
        self._accounts().clear()


class FakeCache:
    def __init__(self):
        self.has_state_changed = False
        self._signed = False

    def add(self, event):
        self.has_state_changed = True

    def serialize(self):
        return '{"accounts": ["user@contoso.com"]}' if self._signed else ""

    def deserialize(self, text):
        self._signed = "user@contoso.com" in text


@pytest.fixture(autouse=True)
def fake_msal(monkeypatch):
    import kb_helper.connectors.msgraph_auth as mod

    monkeypatch.setattr(mod.msal, "PublicClientApplication", FakeMsalApp)
    monkeypatch.setattr(mod.msal, "SerializableTokenCache", FakeCache)
    FakeMsalApp.directory.clear()
    FakeMsalApp.release.clear()
    yield


def test_defaults_use_public_client_and_organizations(tmp_path):
    auth = UserLoginAuth("sharepoint", token_cache_dir=str(tmp_path))
    assert auth.client_id == DEFAULT_PUBLIC_CLIENT_ID
    assert auth.authority.endswith("/organizations")


def test_non_interactive_raises_auth_required(tmp_path):
    auth = UserLoginAuth("sharepoint", token_cache_dir=str(tmp_path), interactive=False)
    token = current_user.set("session-1")
    try:
        with pytest.raises(AuthRequired) as excinfo:
            auth.token()
        assert excinfo.value.connector_name == "sharepoint"
        assert isinstance(excinfo.value, ConnectorError)
        assert auth.status()["state"] == "signed_out"
    finally:
        current_user.reset(token)


def test_web_login_flow_persists_and_is_per_user(tmp_path):
    auth = UserLoginAuth("sharepoint", token_cache_dir=str(tmp_path), interactive=False)
    info = auth.start_login("session-1")
    assert info["user_code"] == "ABCD1234" and info["state"] == "pending"
    assert auth.status("session-1")["state"] == "pending"
    # Starting again while pending returns the same code instead of a new flow.
    assert auth.start_login("session-1")["user_code"] == "ABCD1234"

    FakeMsalApp.release.set()
    deadline = time.time() + 5
    while auth.status("session-1")["state"] == "pending" and time.time() < deadline:
        time.sleep(0.05)
    status = auth.status("session-1")
    assert status["state"] == "signed_in" and status["account"] == "user@contoso.com"
    assert (tmp_path / "session-1.json").exists()

    # Token for that user works; another user is still signed out.
    token = current_user.set("session-1")
    try:
        assert auth.token() == "silent-token"
        assert auth.token() == "silent-token"  # cached in memory
    finally:
        current_user.reset(token)
    assert auth.status("session-2")["signed_in"] is False

    # A fresh instance (server restart) restores the sign-in from disk.
    again = UserLoginAuth("sharepoint", token_cache_dir=str(tmp_path), interactive=False)
    assert again.status("session-1")["signed_in"] is True

    again.sign_out("session-1")
    assert again.status("session-1")["signed_in"] is False
    assert not (tmp_path / "session-1.json").exists()


def test_interactive_terminal_flow(tmp_path, capsys):
    FakeMsalApp.release.set()
    auth = UserLoginAuth("sharepoint", token_cache_dir=str(tmp_path), interactive=True)
    assert auth.token() == "device-token"
    assert "Sign in to sharepoint" in capsys.readouterr().out


def test_app_auth_is_lazy_and_reports_bad_tenant(monkeypatch):
    import kb_helper.connectors.msgraph_auth as mod
    from kb_helper.connectors.msgraph_auth import AppAuth

    calls = []

    class Broken:
        def __init__(self, *a, **k):
            calls.append(1)
            raise ValueError("Unable to get authority configuration")

    monkeypatch.setattr(mod.msal, "ConfidentialClientApplication", Broken)
    auth = AppAuth("bad-tenant", "cid", "secret")
    assert calls == []  # nothing happens at construction time
    with pytest.raises(ConnectorError, match="configuration problem"):
        auth.token()
    with pytest.raises(ConnectorError):
        auth.token()
    assert len(calls) == 1  # the failure is cached briefly instead of hammering Microsoft
