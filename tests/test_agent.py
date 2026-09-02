"""Agent loop tests with a scripted fake Anthropic client (no network)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from kb_helper.agent import ASK_TOOL, READ_TOOL, SEARCH_TOOL, Assistant
from kb_helper.connectors.local_folder import LocalFolderConnector


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(name, input, id_="tu1"):
    return SimpleNamespace(type="tool_use", name=name, input=input, id=id_)


def message(content, stop_reason):
    return SimpleNamespace(content=content, stop_reason=stop_reason, stop_details=None)


class FakeClient:
    """Returns scripted responses in order and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("fake client ran out of scripted responses")
        return self.responses.pop(0)


@pytest.fixture
def connectors(kb_dir):
    return {"docs": LocalFolderConnector("docs", "Test docs", path=str(kb_dir))}


def test_search_read_then_answer(connectors):
    client = FakeClient(
        [
            message([text("Let me check."), tool_use(SEARCH_TOOL, {"query": "deploy staging"}, "a")], "tool_use"),
            message([tool_use(READ_TOOL, {"connector": "docs", "document_id": "deploy-guide.md"}, "b")], "tool_use"),
            message([text("Run the Jenkins job deploy-staging. Source: Deployment Guide.")], "end_turn"),
        ]
    )
    assistant = Assistant(connectors, client=client, model="claude-opus-5", effort="medium")
    history = []
    turn = assistant.respond(history, "how do I deploy to staging?")

    assert turn.kind == "answer"
    assert turn.text.startswith("Let me check.\n\nRun the Jenkins job")
    assert [s.document_id for s in turn.sources] == ["deploy-guide.md"]
    assert [e["type"] for e in turn.events] == ["search", "read"]

    # History alternates correctly and tool results were fed back.
    roles = [m["role"] for m in history]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
    search_result = history[2]["content"][0]
    assert search_result["type"] == "tool_result" and search_result["tool_use_id"] == "a"
    assert "document_id=deploy-guide.md" in search_result["content"]
    read_result = history[4]["content"][0]
    assert "Jenkins job deploy-staging" in read_result["content"]

    # Request shape: cached system prompt, adaptive thinking, effort, fallbacks beta.
    request = client.requests[0]
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "medium"}
    assert request["fallbacks"] == "default" and request["betas"] == ["server-side-fallback-2026-07-01"]
    assert [t["name"] for t in request["tools"]] == [SEARCH_TOOL, READ_TOOL, ASK_TOOL]
    assert request["tools"][0]["input_schema"]["properties"]["connector"]["enum"] == ["docs"]
    assert "Test docs" in request["system"][0]["text"]


def test_ask_user_ends_turn_and_conversation_continues(connectors):
    client = FakeClient(
        [
            message([tool_use(SEARCH_TOOL, {"query": "deploy"}, "a"),
                     tool_use(ASK_TOOL, {"question": "Which environment?", "options": ["staging", "production"], "why": "Steps differ."}, "b")], "tool_use"),
            message([text("For production: create a change ticket first.")], "end_turn"),
        ]
    )
    assistant = Assistant(connectors, client=client, fallbacks=False)
    history = []
    turn = assistant.respond(history, "how do I deploy?")
    assert turn.kind == "question"
    assert turn.text == "Which environment?\n\n_Steps differ._"
    assert turn.options == ["staging", "production"]
    # Both tool calls got a result in one user message, so the history is valid for the next call.
    results = history[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert "shown to the user" in results[1]["content"]

    turn2 = assistant.respond(history, "production")
    assert turn2.kind == "answer" and "change ticket" in turn2.text
    assert history[-2] == {"role": "user", "content": "production"}
    assert "fallbacks" not in client.requests[0]


def test_unknown_connector_and_missing_document_return_errors_to_model(connectors):
    client = FakeClient(
        [
            message([tool_use(SEARCH_TOOL, {"query": "x", "connector": "nope"}, "a"),
                     tool_use(READ_TOOL, {"connector": "docs", "document_id": "missing.md"}, "b")], "tool_use"),
            message([text("done")], "end_turn"),
        ]
    )
    assistant = Assistant(connectors, client=client)
    history = []
    assistant.respond(history, "q")
    results = history[2]["content"]
    assert results[0]["content"].startswith("Error: unknown connector 'nope'")
    assert results[1]["content"].startswith("Error reading missing.md")


def test_refusal_is_reported_without_corrupting_history(connectors):
    client = FakeClient([message([], "refusal")])
    assistant = Assistant(connectors, client=client)
    history = []
    turn = assistant.respond(history, "q")
    assert turn.kind == "answer" and "can't help" in turn.text
    assert history == [{"role": "user", "content": "q"}]


def test_tool_round_limit_forces_final_answer(connectors):
    client = FakeClient(
        [message([tool_use(SEARCH_TOOL, {"query": "loop"}, f"t{i}")], "tool_use") for i in range(2)]
        + [message([text("Best effort answer.")], "end_turn")]
    )
    assistant = Assistant(connectors, client=client, max_tool_rounds=2)
    history = []
    turn = assistant.respond(history, "q")
    assert turn.text == "Best effort answer."
    assert "Answer now" in history[-2]["content"]
    assert "tools" not in client.requests[-1]


def test_read_document_chunks_long_text(connectors, kb_dir):
    (kb_dir / "long.txt").write_text("x" * 50_000, encoding="utf-8")
    assistant = Assistant(connectors, client=FakeClient([]))
    sources = {}
    first = assistant._run_read({"connector": "docs", "document_id": "long.txt"}, sources)
    assert "Characters 0-40000 of 50000" in first and "offset=40000" in first
    second = assistant._run_read({"connector": "docs", "document_id": "long.txt", "offset": 40000}, sources)
    assert "Characters 40000-50000 of 50000" in second and "more available" not in second


def test_api_error_returns_error_turn(connectors):
    import anthropic

    class Boom:
        def __init__(self):
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            raise anthropic.APIConnectionError(request=None)

    assistant = Assistant(connectors, client=Boom())
    history = []
    turn = assistant.respond(history, "q")
    assert turn.kind == "error" and history == []


def test_auth_required_is_surfaced(kb_dir):
    from kb_helper.connectors.msgraph_auth import AuthRequired
    from kb_helper.models import Document, SearchHit
    from kb_helper.connectors import Connector

    class LockedConnector(Connector):
        type_name = "locked"

        def search(self, query, limit=8):
            raise AuthRequired(self.name)

        def fetch(self, document_id):
            raise AuthRequired(self.name)

    client = FakeClient(
        [
            message([tool_use(SEARCH_TOOL, {"query": "deploy"}, "a")], "tool_use"),
            message([text("Please sign in to sharepoint first.")], "end_turn"),
        ]
    )
    assistant = Assistant({"sharepoint": LockedConnector("sharepoint")}, client=client)
    history = []
    turn = assistant.respond(history, "how do I deploy?")
    assert turn.auth_required == ["sharepoint"]
    assert "not signed in to sharepoint" in history[2]["content"][0]["content"]


def test_only_connectors_restricts_search(kb_dir):
    from kb_helper.connectors import Connector
    from kb_helper.models import SearchHit

    class Other(Connector):
        type_name = "other"

        def search(self, query, limit=8):
            return [SearchHit(self.name, "x", "Other doc", "from other")]

        def fetch(self, document_id):
            raise AssertionError("not used")

    connectors = {"docs": LocalFolderConnector("docs", path=str(kb_dir)), "other": Other("other")}
    client = FakeClient(
        [
            message([tool_use(SEARCH_TOOL, {"query": "deploy"}, "a"), tool_use(SEARCH_TOOL, {"query": "deploy", "connector": "other"}, "b")], "tool_use"),
            message([text("done")], "end_turn"),
        ]
    )
    assistant = Assistant(connectors, client=client)
    history = []
    assistant.respond(history, "how do I deploy?", only_connectors=["docs"])
    assert "Search only these sources" in history[0]["content"]
    results = history[2]["content"]
    assert "deploy-guide.md" in results[0]["content"] and "Other doc" not in results[0]["content"]
    assert "limited this question to docs" in results[1]["content"]
