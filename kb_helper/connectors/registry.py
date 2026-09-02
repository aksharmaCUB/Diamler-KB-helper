"""Builds connector instances from configuration.

Adding a new source later means writing a Connector subclass and either registering it here
(``register_connector_type``) or referencing it in config by dotted path
(``type: my_package.my_module:MyConnector``).
"""
from __future__ import annotations

import importlib
from typing import Any

from .base import Connector, ConnectorError

_TYPES: dict[str, type[Connector]] = {}


def register_connector_type(cls: type[Connector], type_name: str | None = None) -> type[Connector]:
    _TYPES[type_name or cls.type_name] = cls
    return cls


def builtin_types() -> dict[str, type[Connector]]:
    if not _TYPES:
        from .local_folder import LocalFolderConnector
        from .sharepoint import SharePointConnector

        register_connector_type(LocalFolderConnector)
        register_connector_type(SharePointConnector)
    return dict(_TYPES)


def resolve_connector_type(type_name: str) -> type[Connector]:
    types = builtin_types()
    if type_name in types:
        return types[type_name]
    if ":" in type_name or "." in type_name:
        module_name, _, attr = type_name.replace(":", ".").rpartition(".")
        try:
            module = importlib.import_module(module_name)
            cls = getattr(module, attr)
        except (ImportError, AttributeError) as exc:
            raise ConnectorError(f"Cannot import connector type {type_name!r}: {exc}") from exc
        if not (isinstance(cls, type) and issubclass(cls, Connector)):
            raise ConnectorError(f"{type_name!r} is not a Connector subclass")
        return cls
    raise ConnectorError(f"Unknown connector type {type_name!r}. Known types: {', '.join(sorted(types))}")


def build_connectors(entries: list[dict[str, Any]]) -> dict[str, Connector]:
    """Instantiate each enabled entry. Entry shape::

        - name: sharepoint          # unique, shown to the model and the user
          type: sharepoint          # builtin name or dotted path
          description: Company IT wiki and runbooks
          enabled: true
          options: {...}            # keyword arguments for the connector class
    """
    connectors: dict[str, Connector] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConnectorError(f"connectors[{index}] must be a mapping")
        if not entry.get("enabled", True):
            continue
        name = entry.get("name")
        type_name = entry.get("type")
        if not name or not type_name:
            raise ConnectorError(f"connectors[{index}] needs both 'name' and 'type'")
        if name in connectors:
            raise ConnectorError(f"Duplicate connector name {name!r}")
        cls = resolve_connector_type(str(type_name))
        options = entry.get("options") or {}
        if not isinstance(options, dict):
            raise ConnectorError(f"connectors[{index}].options must be a mapping")
        try:
            connectors[name] = cls(name=name, description=entry.get("description", ""), **options)
        except TypeError as exc:
            raise ConnectorError(f"Bad options for connector {name!r} ({type_name}): {exc}") from exc
    return connectors
