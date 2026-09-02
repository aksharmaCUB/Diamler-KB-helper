"""Knowledge-source connectors. Each connector turns a backend (SharePoint, a folder, ...) into
`search()` and `fetch()` so the assistant can use it without knowing the details."""
from .base import Connector, ConnectorError
from .registry import build_connectors, register_connector_type, resolve_connector_type

__all__ = [
    "Connector",
    "ConnectorError",
    "build_connectors",
    "register_connector_type",
    "resolve_connector_type",
]
