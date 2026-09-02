import pytest

from kb_helper.connectors import ConnectorError
from kb_helper.connectors.local_folder import LocalFolderConnector


def test_search_ranks_title_matches_first(kb_dir):
    connector = LocalFolderConnector("docs", path=str(kb_dir))
    hits = connector.search("deploy staging")
    assert hits and hits[0].document_id == "deploy-guide.md"
    assert "Jenkins" in hits[0].snippet
    assert all(h.connector == "docs" for h in hits)


def test_search_finds_docx_in_subfolder_and_html(kb_dir):
    connector = LocalFolderConnector("docs", path=str(kb_dir))
    assert connector.search("workday leave")[0].document_id == "hr/leave-policy.docx"
    assert connector.search("vpn")[0].document_id == "notes.html"


def test_search_ignores_unsupported_and_empty_query(kb_dir):
    connector = LocalFolderConnector("docs", path=str(kb_dir))
    assert connector.search("") == []
    assert all(h.document_id != "ignored.bin" for h in connector.search("bin"))


def test_fetch_and_path_safety(kb_dir):
    connector = LocalFolderConnector("docs", path=str(kb_dir))
    document = connector.fetch("ticketing.md")
    assert "ServiceNow" in document.text and document.title == "ticketing"
    with pytest.raises(ConnectorError):
        connector.fetch("../../etc/passwd")
    with pytest.raises(ConnectorError):
        connector.fetch("missing.md")


def test_bad_path_rejected(tmp_path):
    with pytest.raises(ConnectorError):
        LocalFolderConnector("docs", path=str(tmp_path / "nope"))
