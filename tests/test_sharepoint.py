"""SharePoint connector tests against a fake Microsoft Graph served by httpx.MockTransport."""
from __future__ import annotations

import io
import json

import httpx
import pytest

from kb_helper.connectors import ConnectorError
from kb_helper.connectors.sharepoint import SharePointConnector


class FakeAuth:
    def __init__(self):
        self.invalidated = 0

    def token(self):
        return "tok"

    def invalidate(self):
        self.invalidated += 1


def docx_bytes(text: str) -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


SEARCH_RESPONSE = {
    "value": [
        {
            "hitsContainers": [
                {
                    "hits": [
                        {
                            "summary": "Run the <c0>deploy</c0>-staging job ...",
                            "resource": {
                                "@odata.type": "#microsoft.graph.driveItem",
                                "id": "ITEM1",
                                "name": "Deployment Guide.docx",
                                "webUrl": "https://contoso.sharepoint.com/sites/IT/Shared%20Documents/Deployment%20Guide.docx",
                                "lastModifiedDateTime": "2026-05-01T10:00:00Z",
                                "lastModifiedBy": {"user": {"displayName": "Ana"}},
                                "parentReference": {"driveId": "DRIVE1"},
                            },
                        },
                        {
                            "summary": "How to raise a ticket",
                            "resource": {
                                "@odata.type": "#microsoft.graph.listItem",
                                "id": "7",
                                "webUrl": "https://contoso.sharepoint.com/sites/IT/SitePages/Ticketing.aspx",
                                "sharepointIds": {"siteId": "SITE1", "listId": "LIST1", "listItemId": "7", "listItemUniqueId": "PAGE-GUID"},
                                "fields": {"title": "Ticketing"},
                            },
                        },
                        {
                            "summary": "SLA row",
                            "resource": {
                                "@odata.type": "#microsoft.graph.listItem",
                                "id": "3",
                                "webUrl": "https://contoso.sharepoint.com/sites/IT/Lists/SLA/DispForm.aspx?ID=3",
                                "sharepointIds": {"siteId": "SITE1", "listId": "LIST2", "listItemId": "3"},
                                "fields": {"title": "P1"},
                            },
                        },
                    ]
                }
            ]
        }
    ]
}

PAGE_RESPONSE = {
    "id": "PAGE-GUID",
    "name": "Ticketing.aspx",
    "title": "Ticketing",
    "webUrl": "https://contoso.sharepoint.com/sites/IT/SitePages/Ticketing.aspx",
    "canvasLayout": {
        "horizontalSections": [
            {"columns": [{"webparts": [{"innerHtml": "<p>Create a ticket in <b>ServiceNow</b>.</p>"}]}]}
        ],
        "verticalSection": {"webparts": [{"data": {"title": "Owner", "description": "Service Desk"}}]},
    },
}


def make_connector(handler, **kwargs):
    return SharePointConnector(
        "sp",
        tenant_id="t",
        client_id="c",
        auth=FakeAuth(),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_search_api_parsing_and_fetch():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.url.params))
        assert request.headers["Authorization"] == "Bearer tok"
        path = request.url.path
        if path == "/v1.0/search/query":
            body = json.loads(request.content)
            assert body["requests"][0]["region"] == "EMEA"
            assert 'path:"https://contoso.sharepoint.com/sites/IT"' in body["requests"][0]["query"]["queryString"]
            return httpx.Response(200, json=SEARCH_RESPONSE)
        if path == "/v1.0/drives/DRIVE1/items/ITEM1":
            return httpx.Response(200, json={"id": "ITEM1", "name": "Deployment Guide.docx", "size": 1234,
                                             "webUrl": "https://x/Deployment%20Guide.docx",
                                             "file": {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}})
        if path == "/v1.0/drives/DRIVE1/items/ITEM1/content":
            return httpx.Response(200, content=docx_bytes("Run deploy-staging in Jenkins."))
        if path == "/v1.0/sites/SITE1/pages/PAGE-GUID/microsoft.graph.sitePage":
            assert request.url.params["$expand"] == "canvasLayout"
            return httpx.Response(200, json=PAGE_RESPONSE)
        if path == "/v1.0/sites/SITE1/lists/LIST2/items/3":
            return httpx.Response(200, json={"webUrl": "https://x/3", "fields": {"Title": "P1", "Hours": 1, "_hidden": "x", "Body": "<p>Down</p>", "id": "3"}})
        return httpx.Response(404, json={"error": {"code": "itemNotFound", "message": "nope"}})

    connector = make_connector(handler, sites=["https://contoso.sharepoint.com/sites/IT"], search_region="EMEA")
    hits = connector.search("deploy staging", limit=5)
    assert [h.document_id for h in hits] == ["driveItem:DRIVE1:ITEM1", "page:SITE1:PAGE-GUID", "listItem:SITE1:LIST2:3"]
    assert hits[0].snippet == "Run the deploy-staging job ..."
    assert hits[0].author == "Ana" and hits[0].kind == "file"
    assert hits[1].kind == "page" and hits[2].kind == "list_item"

    document = connector.fetch("driveItem:DRIVE1:ITEM1")
    assert "Run deploy-staging in Jenkins." in document.text
    assert document.title == "Deployment Guide.docx"

    page = connector.fetch("page:SITE1:PAGE-GUID")
    assert page.text.startswith("# Ticketing")
    assert "Create a ticket in ServiceNow." in page.text and "Service Desk" in page.text

    item = connector.fetch("listItem:SITE1:LIST2:3")
    assert "Title: P1" in item.text and "Hours: 1" in item.text and "Body: Down" in item.text
    assert "_hidden" not in item.text and "id: 3" not in item.text

    with pytest.raises(ConnectorError):
        connector.fetch("bogus")
    with pytest.raises(ConnectorError, match="404"):
        connector.fetch("driveItem:DRIVE1:MISSING")


def test_falls_back_to_drive_search_when_search_api_forbidden():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1.0/search/query":
            return httpx.Response(403, json={"error": {"code": "AccessDenied", "message": "no search"}})
        if path == "/v1.0/sites/contoso.sharepoint.com:/sites/IT":
            return httpx.Response(200, json={"id": "SITE1"})
        if path == "/v1.0/sites/SITE1/drives":
            return httpx.Response(200, json={"value": [{"id": "DRIVE1", "name": "Documents"}]})
        if path.startswith("/v1.0/drives/DRIVE1/root/search("):
            assert "q='deploy'" in httpx.URL(str(request.url)).path or "deploy" in str(request.url)
            return httpx.Response(200, json={"value": [
                {"id": "A", "name": "Guide.pdf", "webUrl": "https://x/Guide.pdf", "lastModifiedDateTime": "2026-01-01T00:00:00Z"},
                {"id": "B", "name": "Folder", "folder": {}},
                {"id": "C", "name": "photo.png", "webUrl": "https://x/photo.png"},
            ]})
        return httpx.Response(404, json={"error": {"code": "x", "message": "y"}})

    connector = make_connector(handler, sites=["https://contoso.sharepoint.com/sites/IT"])
    hits = connector.search("deploy")
    assert [h.document_id for h in hits] == ["driveItem:DRIVE1:A"]
    # Subsequent searches skip the Search API entirely.
    assert connector.search("deploy")[0].title == "Guide.pdf"
    assert connector.health()["search_api"] is False


def test_search_api_error_without_sites_is_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "AccessDenied", "message": "no search"}})

    connector = make_connector(handler)
    with pytest.raises(ConnectorError, match="AccessDenied"):
        connector.search("deploy")


def test_401_triggers_token_refresh_once():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if len(seen) == 1:
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken", "message": "expired"}})
        return httpx.Response(200, json={"value": []})

    connector = make_connector(handler)
    assert connector.search("x") == []
    assert len(seen) == 2 and connector.auth.invalidated == 1


def test_large_or_unsupported_files_are_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/items/BIG"):
            return httpx.Response(200, json={"name": "big.pdf", "size": 999_000_000})
        if request.url.path.endswith("/items/IMG"):
            return httpx.Response(200, json={"name": "diagram.png", "size": 10})
        return httpx.Response(404, json={"error": {}})

    connector = make_connector(handler)
    with pytest.raises(ConnectorError, match="MB"):
        connector.fetch("driveItem:D:BIG")
    with pytest.raises(ConnectorError, match="not supported"):
        connector.fetch("driveItem:D:IMG")
