"""Unit tests for the neo4j-graphrag text-to-Cypher agent and the /ask endpoint."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from neo4j_graphrag.exceptions import Text2CypherRetrievalError
from neo4j_graphrag.generation.types import RagResultModel
from neo4j_graphrag.llm import AzureOpenAILLM, OpenAILLM
from neo4j_graphrag.types import RetrieverResult, RetrieverResultItem

import api
from agent import (
    AskResult,
    AzureOpenAISettings,
    KnowledgeGraphAgent,
    _record_to_item,
    build_llm,
    fetch_schema_text,
)
from neo4j_client import format_schema


# ── format_schema ────────────────────────────────────────────────────────────
def test_format_schema_renders_sections() -> None:
    schema = {
        "nodes": [{"label": "Aircraft", "properties": ["id", "registration"]}],
        "relationships": [{"startLabels": ["Aircraft"], "type": "HAS_SYSTEM", "endLabels": ["System", "PowerplantSystem"]}],
        "relationshipProperties": [{"type": "FEEDS", "properties": ["fluid"]}],
    }
    text = format_schema(schema)
    assert "Aircraft: id, registration" in text
    assert "(Aircraft)-[HAS_SYSTEM]->(System:PowerplantSystem)" in text
    assert "FEEDS: fluid" in text


# ── fetch_schema_text ────────────────────────────────────────────────────────
class FakeRecord(dict):
    """A neo4j.Record stand-in that supports dict(record)."""


class FakeSchemaDriver:
    """Returns canned rows per schema-introspection query."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def execute_query(self, query: str, database_: str | None = None, routing_: Any = None) -> tuple[list[Any], None, None]:
        self.calls.append((query, database_))
        if "labels(a)" in query:  # relationships
            rows = [FakeRecord(startLabels=["Aircraft"], type="HAS_SYSTEM", endLabels=["System"])]
        elif "type(r)" in query and "endLabels" not in query:  # relationship properties
            rows = [FakeRecord(type="FEEDS", properties=["fluid"])]
        else:  # node samples (labels + properties per node)
            rows = [
                FakeRecord(labels=["Flight"], props={"date": "2026-05-20", "flightTime_hours": 0.5}),
                FakeRecord(labels=["System", "FuelSystem"], props={"name": "Fuel"}),
                FakeRecord(labels=["System", "ElectricalSystem"], props={"name": "Elec"}),
                FakeRecord(labels=["System", "LandingGearSystem"], props={"name": "Gear"}),
                FakeRecord(labels=["System", "IgnitionSystem"], props={"name": "Ign"}),
            ]
        return rows, None, None


def test_fetch_schema_text_uses_database_and_formats() -> None:
    driver = FakeSchemaDriver()
    text = fetch_schema_text(driver, "graph")  # type: ignore[arg-type]
    # Specific labels are enriched with inferred types and example values.
    assert 'date (str, e.g. "2026-05-20")' in text
    assert "flightTime_hours (float, e.g. 0.5)" in text
    # Generic super-labels (>= 4 sibling labels) are trimmed to property names only.
    assert "- System: name\n" in text or text.endswith("- System: name")
    assert "- System: name (str" not in text
    assert "(Aircraft)-[HAS_SYSTEM]->(System)" in text
    assert "FEEDS: fluid" in text
    assert all(db == "graph" for _, db in driver.calls)


# ── _record_to_item ──────────────────────────────────────────────────────────
def test_record_to_item_serialises_and_keeps_record() -> None:
    item = _record_to_item(FakeRecord(flights=6, hours=3.0))
    assert item.metadata == {"record": {"flights": 6, "hours": 3.0}}
    assert '"flights": 6' in item.content


# ── AzureOpenAISettings.from_env ─────────────────────────────────────────────
def test_azure_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)
    settings = AzureOpenAISettings.from_env()
    assert settings.deployment == "gpt-4o"
    assert settings.api_version  # default applied


def test_azure_settings_from_env_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="Missing required environment variable"):
        AzureOpenAISettings.from_env()


# ── build_llm ────────────────────────────────────────────────────────────────
def test_build_llm_uses_openai_client_for_v1_endpoint() -> None:
    settings = AzureOpenAISettings("https://res.openai.azure.com/openai/v1", "key", "gpt-5.4", "2024-10-21")
    llm = build_llm(settings)
    assert isinstance(llm, OpenAILLM)


def test_build_llm_uses_azure_client_for_classic_endpoint() -> None:
    settings = AzureOpenAISettings("https://res.openai.azure.com/", "key", "gpt-4o", "2024-10-21")
    llm = build_llm(settings)
    assert isinstance(llm, AzureOpenAILLM)


# ── KnowledgeGraphAgent.ask (result extraction) ──────────────────────────────
class FakeDriver:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeRag:
    """Stands in for GraphRAG: returns a canned result or raises."""

    def __init__(self, *, result: RagResultModel | None = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def search(self, query_text: str, return_context: bool | None = None) -> RagResultModel:
        self.calls.append(query_text)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _make_agent(rag: FakeRag) -> tuple[KnowledgeGraphAgent, FakeDriver]:
    agent = object.__new__(KnowledgeGraphAgent)
    driver = FakeDriver()
    agent._driver = driver  # type: ignore[assignment]
    agent._rag = rag  # type: ignore[assignment]
    return agent, driver


async def test_ask_extracts_answer_cypher_and_records() -> None:
    result = RagResultModel(
        answer="There are 6 flights.",
        retriever_result=RetrieverResult(
            items=[RetrieverResultItem(content="{}", metadata={"record": {"flights": 6}})],
            metadata={"cypher": "MATCH (f:Flight) RETURN count(f) AS flights"},
        ),
    )
    rag = FakeRag(result=result)
    agent, _ = _make_agent(rag)
    ask_result = await agent.ask("How many flights?")
    assert isinstance(ask_result, AskResult)
    assert ask_result.answer == "There are 6 flights."
    assert ask_result.cypher_used == ["MATCH (f:Flight) RETURN count(f) AS flights"]
    assert ask_result.records == [{"flights": 6}]
    assert rag.calls == ["How many flights?"]


async def test_ask_degrades_gracefully_on_retrieval_error() -> None:
    rag = FakeRag(error=Text2CypherRetrievalError("bad cypher"))
    agent, _ = _make_agent(rag)
    ask_result = await agent.ask("nonsense")
    assert ask_result.cypher_used == []
    assert ask_result.records == []
    assert "couldn't" in ask_result.answer.lower()


async def test_ask_degrades_gracefully_on_unexpected_error() -> None:
    rag = FakeRag(error=RuntimeError("LLM down"))
    agent, _ = _make_agent(rag)
    ask_result = await agent.ask("anything")
    assert ask_result.cypher_used == []
    assert "couldn't" in ask_result.answer.lower()


def test_close_closes_driver() -> None:
    agent, driver = _make_agent(FakeRag(error=RuntimeError()))
    agent.close()
    assert driver.closed is True


# ── /ask endpoint ────────────────────────────────────────────────────────────
class FakeKGAgent:
    async def ask(self, question: str) -> AskResult:
        return AskResult(answer="42", cypher_used=["MATCH (n) RETURN count(n)"], records=[{"count": 1}])


async def test_ask_endpoint_returns_answer() -> None:
    api.app.dependency_overrides[api.get_agent] = lambda: FakeKGAgent()
    transport = ASGITransport(app=api.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/ask", json={"question": "How many nodes?"})
    finally:
        api.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"answer": "42", "cypher_used": ["MATCH (n) RETURN count(n)"], "records": [{"count": 1}]}


async def test_ask_endpoint_503_when_agent_unconfigured() -> None:
    # No dependency override and no app.state.agent -> get_agent raises 503.
    api.app.state.agent = None
    transport = ASGITransport(app=api.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ask", json={"question": "anything"})
    assert response.status_code == 503


async def test_ask_endpoint_rejects_empty_question() -> None:
    api.app.dependency_overrides[api.get_agent] = lambda: FakeKGAgent()
    transport = ASGITransport(app=api.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/ask", json={"question": ""})
    finally:
        api.app.dependency_overrides.clear()
    assert response.status_code == 422
