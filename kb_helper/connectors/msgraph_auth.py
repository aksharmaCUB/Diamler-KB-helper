"""Token acquisition for Microsoft Graph via MSAL.

Two ways to authenticate:

* ``UserLoginAuth`` (default) - the person signs in with their own work account. Uses the
  OAuth *device code* flow: the chatbot shows a short code and a Microsoft URL, the user logs in
  there (MFA works), and the helper receives a token that can only see what that account can see.
  No app registration or client secret is required because it uses Microsoft's own public
  "Microsoft Graph Command Line Tools" client id; you may substitute your own public client id.
  Tokens are cached per user (a web session, or "local" for the CLI) and refreshed silently.

* ``AppAuth`` - client credentials for an app registration with *application* permissions
  (Sites.Read.All, Files.Read.All) and a secret. Unattended and shared by all users.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import msal

from ..context import current_user
from .base import ConnectorError

# Microsoft's first-party public client used by "Microsoft Graph PowerShell / CLI". It is
# multi-tenant, allows device-code sign-in, and needs no registration in your tenant.
DEFAULT_PUBLIC_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
GRAPH_APP_SCOPES = ["https://graph.microsoft.com/.default"]
GRAPH_DELEGATED_SCOPES = ["Sites.Read.All", "Files.Read.All", "User.Read"]
_SAFE_KEY = re.compile(r"[^A-Za-z0-9_.-]")


class AuthRequired(ConnectorError):
    """The current user has not signed in yet; the UI should offer a sign-in."""

    def __init__(self, connector_name: str) -> None:
        super().__init__(f"You are not signed in to {connector_name}. Use the sign-in button to connect your account.")
        self.connector_name = connector_name


def _authority(host: str, tenant: str) -> str:
    return f"{host.rstrip('/')}/{tenant}"


class AppAuth:
    """Client-credentials flow (application permissions). The MSAL app is created lazily so that
    configuring a connector never blocks on the network; problems surface on first use."""

    ERROR_CACHE_SECONDS = 60.0

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        scopes: list[str] | None = None,
        authority_host: str = "https://login.microsoftonline.com",
    ) -> None:
        if not (tenant_id and client_id and client_secret):
            raise ConnectorError("client_credentials auth needs tenant_id, client_id and client_secret")
        self.scopes = scopes or GRAPH_APP_SCOPES
        self.client_id = client_id
        self.client_secret = client_secret
        self.authority = _authority(authority_host, tenant_id)
        self._app: msal.ConfidentialClientApplication | None = None
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self._last_error: tuple[float, str] | None = None

    def _application(self) -> msal.ConfidentialClientApplication:
        if self._app is None:
            try:
                self._app = msal.ConfidentialClientApplication(
                    self.client_id, authority=self.authority, client_credential=self.client_secret
                )
            except ValueError as exc:  # bad tenant / authority unreachable
                raise ConnectorError(f"Microsoft sign-in configuration problem: {exc}") from exc
        return self._app

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            if self._last_error and time.time() - self._last_error[0] < self.ERROR_CACHE_SECONDS:
                raise ConnectorError(self._last_error[1])
            try:
                result = self._application().acquire_token_for_client(scopes=self.scopes) or {}
                if "access_token" not in result:
                    raise ConnectorError(
                        f"Microsoft Graph authentication failed: {result.get('error')}: {result.get('error_description')}"
                    )
            except ConnectorError as exc:
                self._last_error = (time.time(), str(exc))
                raise
            self._last_error = None
            self._token = result["access_token"]
            self._expires_at = time.time() + float(result.get("expires_in", 3600))
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0
            self._last_error = None

    def status(self) -> dict[str, Any]:
        return {"mode": "client_credentials", "signed_in": True}


class UserLoginAuth:
    """Per-user device-code sign-in with persistent token caches."""

    def __init__(
        self,
        connector_name: str,
        tenant_id: str = "organizations",
        client_id: str = DEFAULT_PUBLIC_CLIENT_ID,
        scopes: list[str] | None = None,
        token_cache_dir: str = ".kb_helper_tokens",
        interactive: bool = True,
        authority_host: str = "https://login.microsoftonline.com",
    ) -> None:
        self.connector_name = connector_name
        self.client_id = client_id or DEFAULT_PUBLIC_CLIENT_ID
        self.authority = _authority(authority_host, tenant_id or "organizations")
        self.scopes = scopes or GRAPH_DELEGATED_SCOPES
        self.cache_dir = Path(token_cache_dir)
        self.interactive = interactive  # True: block and print the code in the terminal (CLI)
        self._lock = threading.Lock()
        self._caches: dict[str, msal.SerializableTokenCache] = {}
        self._apps: dict[str, msal.PublicClientApplication] = {}
        self._flows: dict[str, dict[str, Any]] = {}  # user_key -> {"flow", "thread", "result", "error"}
        self._tokens: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------ cache plumbing
    def _cache_path(self, user_key: str) -> Path:
        return self.cache_dir / f"{_SAFE_KEY.sub('_', user_key)}.json"

    def _app_for(self, user_key: str) -> msal.PublicClientApplication:
        with self._lock:
            if user_key not in self._apps:
                cache = msal.SerializableTokenCache()
                path = self._cache_path(user_key)
                if path.exists():
                    try:
                        cache.deserialize(path.read_text(encoding="utf-8"))
                    except (ValueError, OSError):
                        pass
                self._caches[user_key] = cache
                try:
                    self._apps[user_key] = msal.PublicClientApplication(
                        self.client_id, authority=self.authority, token_cache=cache
                    )
                except ValueError as exc:  # bad tenant / authority unreachable
                    self._caches.pop(user_key, None)
                    raise ConnectorError(f"Microsoft sign-in configuration problem: {exc}") from exc
            return self._apps[user_key]

    def _persist(self, user_key: str) -> None:
        cache = self._caches.get(user_key)
        if cache is None or not cache.has_state_changed:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._cache_path(user_key)
        path.write_text(cache.serialize(), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------ token access
    def _silent(self, user_key: str) -> dict[str, Any] | None:
        app = self._app_for(user_key)
        accounts = app.get_accounts()
        if not accounts:
            return None
        result = app.acquire_token_silent(self.scopes, account=accounts[0])
        self._persist(user_key)
        return result

    def token(self) -> str:
        user_key = current_user.get()
        cached = self._tokens.get(user_key)
        if cached and time.time() < cached[1] - 60:
            return cached[0]
        result = self._silent(user_key)
        if not result and self.interactive:
            result = self._device_flow_blocking(user_key)
        if not result or "access_token" not in result:
            if result and result.get("error"):
                raise ConnectorError(
                    f"Microsoft sign-in failed: {result.get('error')}: {result.get('error_description')}"
                )
            raise AuthRequired(self.connector_name)
        self._tokens[user_key] = (result["access_token"], time.time() + float(result.get("expires_in", 3600)))
        return result["access_token"]

    def invalidate(self) -> None:
        self._tokens.pop(current_user.get(), None)

    # ------------------------------------------------------------------ device code flow
    def _device_flow_blocking(self, user_key: str) -> dict[str, Any]:
        app = self._app_for(user_key)
        flow = app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            raise ConnectorError(f"Could not start Microsoft sign-in: {json.dumps(flow, indent=2)}")
        print(f"\n=== Sign in to {self.connector_name} with your work account ===")
        print(flow["message"])
        print("=" * 60 + "\n", flush=True)
        result = app.acquire_token_by_device_flow(flow)
        self._persist(user_key)
        return result or {}

    def start_login(self, user_key: str) -> dict[str, Any]:
        """Begin a device-code sign-in for ``user_key`` in the background; returns what to show."""
        with self._lock:
            pending = self._flows.get(user_key)
            if pending and pending["thread"].is_alive():
                flow = pending["flow"]
                return self._describe_flow(flow)
        app = self._app_for(user_key)
        flow = app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            raise ConnectorError(f"Could not start Microsoft sign-in: {flow.get('error_description') or flow}")
        state: dict[str, Any] = {"flow": flow, "result": None, "error": None}

        def run() -> None:
            try:
                state["result"] = app.acquire_token_by_device_flow(flow) or {}
                self._persist(user_key)
            except Exception as exc:  # noqa: BLE001
                state["error"] = str(exc)

        thread = threading.Thread(target=run, name=f"msal-device-flow-{user_key[:8]}", daemon=True)
        state["thread"] = thread
        with self._lock:
            self._flows[user_key] = state
        thread.start()
        return self._describe_flow(flow)

    @staticmethod
    def _describe_flow(flow: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "pending",
            "user_code": flow["user_code"],
            "verification_uri": flow.get("verification_uri_complete") or flow["verification_uri"],
            "message": flow["message"],
            "expires_in": flow.get("expires_in"),
        }

    def status(self, user_key: str | None = None) -> dict[str, Any]:
        user_key = user_key or current_user.get()
        pending = self._flows.get(user_key)
        if pending and pending["thread"].is_alive():
            return self._describe_flow(pending["flow"])
        if pending:
            result, error = pending["result"], pending["error"]
            with self._lock:
                self._flows.pop(user_key, None)
            if error or (result is not None and "access_token" not in result):
                detail = error or f"{result.get('error')}: {result.get('error_description')}"
                return {"mode": "user", "state": "error", "signed_in": False, "error": detail}
        account = self.account(user_key)
        return {"mode": "user", "state": "signed_in" if account else "signed_out", "signed_in": bool(account), "account": account}

    def account(self, user_key: str | None = None) -> str | None:
        accounts = self._app_for(user_key or current_user.get()).get_accounts()
        return accounts[0].get("username") if accounts else None

    def sign_out(self, user_key: str | None = None) -> None:
        user_key = user_key or current_user.get()
        app = self._app_for(user_key)
        for account in app.get_accounts():
            app.remove_account(account)
        self._persist(user_key)
        self._tokens.pop(user_key, None)
        with self._lock:
            self._apps.pop(user_key, None)
            self._caches.pop(user_key, None)
            self._flows.pop(user_key, None)
        path = self._cache_path(user_key)
        if path.exists():
            path.unlink()
