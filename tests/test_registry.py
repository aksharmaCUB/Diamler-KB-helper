import pytest

from kb_helper.connectors import Connector, ConnectorError, build_connectors, build_connectors_lenient, register_connector_type, resolve_connector_type
from kb_helper.connectors.local_folder import LocalFolderConnector
from kb_helper.models import Document, SearchHit


class DummyConnector(Connector):
    type_name = "dummy"

    def __init__(self, name, description="", *, greeting="hi"):
        super().__init__(name, description)
        self.greeting = greeting

    def search(self, query, limit=8):
        return [SearchHit(self.name, "1", "Dummy", self.greeting)]

    def fetch(self, document_id):
        return Document(self.name, document_id, "Dummy", self.greeting)


def test_builtin_types_resolve():
    assert resolve_connector_type("local_folder") is LocalFolderConnector
    assert resolve_connector_type("sharepoint").type_name == "sharepoint"


def test_dotted_path_and_registration():
    cls = resolve_connector_type("tests.test_registry:DummyConnector")
    assert cls is DummyConnector
    register_connector_type(DummyConnector)
    assert resolve_connector_type("dummy") is DummyConnector


def test_build_connectors(kb_dir):
    connectors = build_connectors(
        [
            {"name": "docs", "type": "local_folder", "options": {"path": str(kb_dir)}},
            {"name": "off", "type": "local_folder", "enabled": False, "options": {"path": "/nowhere"}},
            {"name": "d", "type": "tests.test_registry:DummyConnector", "description": "x", "options": {"greeting": "yo"}},
        ]
    )
    assert list(connectors) == ["docs", "d"]
    assert connectors["d"].search("q")[0].snippet == "yo"


@pytest.mark.parametrize(
    "entries",
    [
        [{"name": "a", "type": "nope"}],
        [{"type": "local_folder"}],
        [{"name": "a", "type": "local_folder", "options": {"bogus": 1}}],
        [{"name": "a", "type": "tests.test_registry:DummyConnector"}, {"name": "a", "type": "tests.test_registry:DummyConnector"}],
        [{"name": "a", "type": "os.path:join"}],
    ],
)
def test_bad_entries(entries):
    with pytest.raises(ConnectorError):
        build_connectors(entries)


def test_build_connectors_lenient_reports_per_entry(kb_dir):
    connectors, errors = build_connectors_lenient(
        [
            {"name": "docs", "type": "local_folder", "options": {"path": str(kb_dir)}},
            {"name": "broken", "type": "local_folder", "options": {"path": "/definitely/missing"}},
            {"name": "weird", "type": "no_such_type"},
            {"name": "off", "type": "no_such_type", "enabled": False},
        ]
    )
    assert list(connectors) == ["docs"]
    assert "not a directory" in errors["broken"]
    assert "Unknown connector type" in errors["weird"]
    assert "off" not in errors
