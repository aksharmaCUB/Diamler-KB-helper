"""Request-scoped context shared with connectors.

``current_user`` identifies who is asking (a web session id, or "local" for the CLI) so that
connectors using per-user sign-in can pick the right credentials.
"""
from __future__ import annotations

from contextvars import ContextVar

LOCAL_USER = "local"
current_user: ContextVar[str] = ContextVar("kb_helper_current_user", default=LOCAL_USER)
