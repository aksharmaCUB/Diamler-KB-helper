"""SharePoint Online connector built on the Microsoft Graph API.

Search strategy
---------------
1. Microsoft Search API (``POST /search/query``) over driveItems (files) and listItems (pages,
   list rows). This is what the SharePoint search box uses, so ranking and snippets are good.
   With application permissions Microsoft requires a ``region`` value (``search_region``).
2. If the Search API is unavailable (403/400) and ``sites`` are configured, fall back to
   per-library ``/drives/{id}/root/search(q=...)`` calls on those sites.

Document ids handed to the model are opaque strings::

    driveItem:<driveId>:<itemId>          a file in a document library
    listItem:<siteId>:<listId>:<itemId>   a row of a SharePoint list
    page:<siteId>:<pageId>                a modern site page (.aspx)
"""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from ..models import Document, SearchHit
from .base import Connector, ConnectorError
from .extract import extension_of, extract_text, html_to_text, is_supported
from .msgraph_auth import DEFAULT_PUBLIC_CLIENT_ID, AppAuth, AuthRequired, UserLoginAuth

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
MAX_DOWNLOAD_BYTES = 30_000_000
_INTERNAL_FIELD = re.compile(r"^(_|@odata|ContentType$|Attachments$|Edit$|LinkTitle|ItemChildCount|FolderChildCount|AppAuthor|AppEditor|ComplianceAssetId|id$)")


class SharePointConnector(Connector):
    type_name = "sharepoint"

    def __init__(
        self,
        name: str,
        description: str = "",
        *,
        auth_mode: str = "user",
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        sites: list[str] | None = None,
        search_region: str | None = None,
        token_cache_dir: str = ".kb_helper_tokens",
        timeout: float = 30.0,
        auth: Any | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(name, description or "SharePoint Online (documents, pages and lists)")
        self.sites = [s.strip() for s in (sites or []) if s and s.strip()]
        self.search_region = search_region
        if auth_mode == "device_code":  # older name
            auth_mode = "user"
        self.auth_mode = auth_mode
        if auth is not None:
            self.auth = auth
        elif auth_mode == "user":
            self.auth = UserLoginAuth(
                connector_name=name,
                tenant_id=tenant_id or "organizations",
                client_id=client_id or DEFAULT_PUBLIC_CLIENT_ID,
                token_cache_dir=token_cache_dir,
            )
        elif auth_mode == "client_credentials":
            self.auth = AppAuth(tenant_id=tenant_id or "", client_id=client_id or "", client_secret=client_secret or "")
        else:
            raise ConnectorError(f"auth_mode must be 'user' or 'client_credentials', got {auth_mode!r}")
        self._http = httpx.Client(base_url=GRAPH_BASE, timeout=timeout, follow_redirects=True, transport=transport)
        self._site_ids: dict[str, str] = {}
        self._drives: dict[str, list[dict[str, Any]]] = {}
        self._search_api_ok: bool | None = None

    # ------------------------------------------------------------------ HTTP plumbing
    def _request(self, method: str, url: str, *, retry_auth: bool = True, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self.auth.token()}"
        try:
            response = self._http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Microsoft Graph request failed: {exc}") from exc
        if response.status_code == 401 and retry_auth:
            self.auth.invalidate()
            return self._request(method, url, retry_auth=False, headers=headers, **kwargs)
        if response.status_code == 429:
            wait = float(response.headers.get("Retry-After", "2"))
            time.sleep(min(wait, 10))
            return self._request(method, url, retry_auth=False, headers=headers, **kwargs)
        return response

    def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = self._request(method, url, **kwargs)
        if response.status_code >= 400:
            raise ConnectorError(self._error_text(response))
        return response.json()

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        try:
            error = response.json().get("error", {})
            detail = f"{error.get('code')}: {error.get('message')}"
        except ValueError:
            detail = response.text[:300]
        return f"Microsoft Graph returned {response.status_code} ({detail})"

    # ------------------------------------------------------------------ site resolution
    def _site_id(self, site: str) -> str:
        """Accepts a full URL (https://x.sharepoint.com/sites/IT) or 'x.sharepoint.com:/sites/IT'."""
        if site in self._site_ids:
            return self._site_ids[site]
        if site.startswith("http"):
            parsed = urlparse(site)
            path = parsed.path.rstrip("/") or "/"
            locator = f"{parsed.netloc}:{path}" if path != "/" else parsed.netloc
        else:
            locator = site
        data = self._json("GET", f"/sites/{locator}", params={"$select": "id,webUrl,displayName"})
        self._site_ids[site] = data["id"]
        return data["id"]

    def _site_drives(self, site_id: str) -> list[dict[str, Any]]:
        if site_id not in self._drives:
            data = self._json("GET", f"/sites/{site_id}/drives", params={"$select": "id,name,webUrl"})
            self._drives[site_id] = data.get("value", [])
        return self._drives[site_id]

    # ------------------------------------------------------------------ search
    def search(self, query: str, limit: int = 8) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        if self._search_api_ok is not False:
            try:
                hits = self._search_api(query, limit)
                self._search_api_ok = True
                return hits
            except AuthRequired:
                raise
            except ConnectorError as exc:
                if not self.sites:
                    raise
                self._search_api_ok = False
                self._search_api_error = str(exc)
        return self._drive_search(query, limit)

    def _search_api(self, query: str, limit: int) -> list[SearchHit]:
        query_string = query
        if self.sites:
            paths = " OR ".join(f'path:"{self._site_url(site)}"' for site in self.sites)
            query_string = f"({query}) AND ({paths})"
        request: dict[str, Any] = {
            "entityTypes": ["driveItem", "listItem"],
            "query": {"queryString": query_string},
            "from": 0,
            "size": max(1, min(limit, 25)),
        }
        if self.search_region:
            request["region"] = self.search_region
        data = self._json("POST", "/search/query", json={"requests": [request]})
        hits: list[SearchHit] = []
        for container in data.get("value", []):
            for bucket in container.get("hitsContainers", []):
                for hit in bucket.get("hits", []):
                    parsed = self._parse_search_hit(hit)
                    if parsed:
                        hits.append(parsed)
        return hits[:limit]

    def _site_url(self, site: str) -> str:
        if site.startswith("http"):
            return site.rstrip("/")
        host, _, path = site.partition(":")
        return f"https://{host}{path}".rstrip("/")

    def _parse_search_hit(self, hit: dict[str, Any]) -> SearchHit | None:
        resource = hit.get("resource") or {}
        odata_type = resource.get("@odata.type", "")
        summary = html_to_text(hit.get("summary") or "")
        web_url = resource.get("webUrl")
        modified = resource.get("lastModifiedDateTime")
        author = ((resource.get("lastModifiedBy") or {}).get("user") or {}).get("displayName")

        if odata_type.endswith("driveItem"):
            drive_id = (resource.get("parentReference") or {}).get("driveId")
            item_id = resource.get("id")
            if not drive_id or not item_id:
                return None
            name = resource.get("name") or (web_url or "").rsplit("/", 1)[-1]
            kind = "page" if name.lower().endswith(".aspx") else "file"
            return SearchHit(
                connector=self.name,
                document_id=f"driveItem:{drive_id}:{item_id}",
                title=name,
                snippet=summary,
                url=web_url,
                kind=kind,
                modified=modified,
                author=author,
            )

        if odata_type.endswith("listItem"):
            sp_ids = resource.get("sharepointIds") or {}
            site_id = sp_ids.get("siteId") or (resource.get("parentReference") or {}).get("siteId")
            list_id = sp_ids.get("listId")
            item_id = sp_ids.get("listItemId") or resource.get("id")
            fields = resource.get("fields") or {}
            title = fields.get("title") or fields.get("Title") or (web_url or "").rsplit("/", 1)[-1] or "List item"
            if web_url and web_url.lower().endswith(".aspx") and sp_ids.get("listItemUniqueId") and site_id:
                return SearchHit(
                    connector=self.name,
                    document_id=f"page:{site_id}:{sp_ids['listItemUniqueId']}",
                    title=title,
                    snippet=summary,
                    url=web_url,
                    kind="page",
                    modified=modified,
                    author=author,
                )
            if site_id and list_id and item_id:
                return SearchHit(
                    connector=self.name,
                    document_id=f"listItem:{site_id}:{list_id}:{item_id}",
                    title=title,
                    snippet=summary,
                    url=web_url,
                    kind="list_item",
                    modified=modified,
                    author=author,
                )
        return None

    def _drive_search(self, query: str, limit: int) -> list[SearchHit]:
        hits: list[SearchHit] = []
        escaped = query.replace("'", "''")
        for site in self.sites:
            site_id = self._site_id(site)
            for drive in self._site_drives(site_id):
                data = self._json(
                    "GET",
                    f"/drives/{drive['id']}/root/search(q='{quote(escaped)}')",
                    params={"$top": limit, "$select": "id,name,webUrl,lastModifiedDateTime,lastModifiedBy,parentReference,file,folder"},
                )
                for item in data.get("value", []):
                    if "folder" in item or not is_supported(item.get("name", "")):
                        continue
                    name = item["name"]
                    hits.append(
                        SearchHit(
                            connector=self.name,
                            document_id=f"driveItem:{drive['id']}:{item['id']}",
                            title=name,
                            snippet=f"{drive.get('name', 'Library')} - {name}",
                            url=item.get("webUrl"),
                            kind="page" if name.lower().endswith(".aspx") else "file",
                            modified=item.get("lastModifiedDateTime"),
                            author=((item.get("lastModifiedBy") or {}).get("user") or {}).get("displayName"),
                        )
                    )
        hits.sort(key=lambda h: h.modified or "", reverse=True)
        return hits[:limit]

    # ------------------------------------------------------------------ fetch
    def fetch(self, document_id: str) -> Document:
        kind, _, rest = document_id.partition(":")
        parts = rest.split(":")
        if kind == "driveItem" and len(parts) == 2:
            return self._fetch_drive_item(*parts)
        if kind == "listItem" and len(parts) == 3:
            return self._fetch_list_item(*parts)
        if kind == "page" and len(parts) == 2:
            return self._fetch_page(*parts)
        raise ConnectorError(f"Unrecognised SharePoint document id {document_id!r}")

    def _fetch_drive_item(self, drive_id: str, item_id: str) -> Document:
        meta = self._json(
            "GET",
            f"/drives/{drive_id}/items/{item_id}",
            params={"$select": "id,name,webUrl,size,file,lastModifiedDateTime,sharepointIds,parentReference"},
        )
        name = meta.get("name", item_id)
        web_url = meta.get("webUrl")
        document_id = f"driveItem:{drive_id}:{item_id}"
        base_meta = {"modified": meta.get("lastModifiedDateTime"), "size": meta.get("size")}

        if name.lower().endswith(".aspx"):
            sp_ids = meta.get("sharepointIds") or {}
            if sp_ids.get("siteId") and sp_ids.get("listItemUniqueId"):
                try:
                    page = self._fetch_page(sp_ids["siteId"], sp_ids["listItemUniqueId"])
                    page.document_id = document_id
                    return page
                except ConnectorError:
                    pass  # fall through to raw download

        size = int(meta.get("size") or 0)
        if size > MAX_DOWNLOAD_BYTES:
            raise ConnectorError(f"{name} is {size // 1_000_000} MB, above the {MAX_DOWNLOAD_BYTES // 1_000_000} MB limit")
        if not is_supported(name):
            raise ConnectorError(f"{name}: file type {extension_of(name) or '(none)'} is not supported for text extraction")
        response = self._request("GET", f"/drives/{drive_id}/items/{item_id}/content")
        if response.status_code >= 400:
            raise ConnectorError(self._error_text(response))
        mime = (meta.get("file") or {}).get("mimeType") or response.headers.get("content-type")
        text = extract_text(response.content, name, mime)
        if not text:
            raise ConnectorError(f"{name}: no text could be extracted")
        return Document(
            connector=self.name,
            document_id=document_id,
            title=name,
            text=text,
            url=web_url,
            kind="page" if name.lower().endswith(".aspx") else "file",
            metadata=base_meta,
        )

    def _fetch_list_item(self, site_id: str, list_id: str, item_id: str) -> Document:
        data = self._json("GET", f"/sites/{site_id}/lists/{list_id}/items/{item_id}", params={"$expand": "fields"})
        fields = data.get("fields") or {}
        lines = []
        for key, value in fields.items():
            if _INTERNAL_FIELD.match(key) or value in (None, "", [], {}):
                continue
            if isinstance(value, str) and "<" in value and ">" in value:
                value = html_to_text(value)
            lines.append(f"{key}: {value}")
        title = fields.get("Title") or f"List item {item_id}"
        return Document(
            connector=self.name,
            document_id=f"listItem:{site_id}:{list_id}:{item_id}",
            title=title,
            text="\n".join(lines) or "(empty list item)",
            url=data.get("webUrl"),
            kind="list_item",
            metadata={"modified": data.get("lastModifiedDateTime")},
        )

    def _fetch_page(self, site_id: str, page_id: str) -> Document:
        data = self._json(
            "GET",
            f"/sites/{site_id}/pages/{page_id}/microsoft.graph.sitePage",
            params={"$expand": "canvasLayout"},
        )
        parts: list[str] = []
        if data.get("title"):
            parts.append(f"# {data['title']}")
        if data.get("description"):
            parts.append(data["description"])
        layout = data.get("canvasLayout") or {}
        sections = list(layout.get("horizontalSections") or [])
        vertical = layout.get("verticalSection")
        if vertical:
            sections.append({"columns": [vertical]})
        for section in sections:
            for column in section.get("columns") or []:
                for part in column.get("webparts") or []:
                    parts.append(self._webpart_text(part))
        text = "\n\n".join(p for p in parts if p)
        if not text.strip():
            raise ConnectorError(f"Page {data.get('name', page_id)} has no readable text")
        return Document(
            connector=self.name,
            document_id=f"page:{site_id}:{page_id}",
            title=data.get("title") or data.get("name") or "Site page",
            text=text,
            url=data.get("webUrl"),
            kind="page",
            metadata={"modified": data.get("lastModifiedDateTime")},
        )

    @staticmethod
    def _webpart_text(part: dict[str, Any]) -> str:
        if part.get("innerHtml"):
            return html_to_text(part["innerHtml"])
        data = part.get("data") or {}
        chunks: list[str] = []
        for key in ("title", "description"):
            if data.get(key):
                chunks.append(str(data[key]))
        props = data.get("properties") or {}
        for value in props.values():
            if isinstance(value, str) and len(value) > 20:
                chunks.append(html_to_text(value) if "<" in value else value)
        return "\n".join(chunks)

    # ------------------------------------------------------------------ misc
    def login_provider(self) -> UserLoginAuth | None:
        return self.auth if isinstance(self.auth, UserLoginAuth) else None

    def health(self) -> dict[str, Any]:
        info: dict[str, Any] = {"ok": True, "auth_mode": self.auth_mode, "sites": self.sites, "search_api": self._search_api_ok}
        if isinstance(self.auth, UserLoginAuth):
            info.update(self.auth.status())
            return info
        try:
            self.auth.token()
        except ConnectorError as exc:
            return {"ok": False, "error": str(exc), "auth_mode": self.auth_mode}
        return info
