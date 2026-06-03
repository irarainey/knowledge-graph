"""Unit tests for the knowledge-graph agent (text-to-Cypher GraphRAG) and /ask."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import api
from agent import (
    AskResult,
    AzureOpenAISettings,
    KnowledgeGraphAgent,
    azure_client_kwargs,
    ensure_read_only,
    format_schema,
)


# ── ensure_read_only ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "cypher",
    [
        "CREATE (n:Foo) RETURN n",
        "MATCH (n) DETACH DELETE n",
        "MATCH (n) SET n.x = 1",
        "MATCH (n) REMOVE n.x",
        "MERGE (n:Foo {id: 1})",
        "LOAD CSV FROM 'x' AS row RETURN row",
    ],
)
def test_ensure_read_only_rejects_writes(cypher: str) -> None:
    with pytest.raises(ValueError, match="read-only"):
        ensure_read_only(cypher)


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (n:Aircraft) RETURN n",
        "MATCH (f:Flight) WHERE date(f.date) >= date($since) RETURN sum(f.flightTime_hours)",
        "MATCH (a)-[:HAS_SYSTEM]->(s) RETURN a, s LIMIT 10",
    ],
)
def test_ensure_read_only_allows_reads(cypher: str) -> None:
    ensure_read_only(cypher)  # should not raise


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


# ── azure_client_kwargs ──────────────────────────────────────────────────────
def test_azure_client_kwargs_v1_uses_base_url() -> None:
    settings = AzureOpenAISettings("https://res.openai.azure.com/openai/v1", "key", "gpt-5.4", "2024-10-21")
    kwargs = azure_client_kwargs(settings)
    assert kwargs["base_url"] == "https://res.openai.azure.com/openai/v1"
    assert "azure_endpoint" not in kwargs
    assert kwargs["model"] == "gpt-5.4"


def test_azure_client_kwargs_classic_uses_azure_endpoint() -> None:
    settings = AzureOpenAISettings("https://res.openai.azure.com/", "key", "gpt-4o", "2024-10-21")
    kwargs = azure_client_kwargs(settings)
    assert kwargs["azure_endpoint"] == "https://res.openai.azure.com"
    assert kwargs["api_version"] == "2024-10-21"
    assert "base_url" not in kwargs


# ── KnowledgeGraphAgent tool behaviour ───────────────────────────────────────
class FakeNeo4j:
    """Captures read queries and returns canned schema/rows."""

    def __init__(self) -> None:
        self.read_calls: list[tuple[str, dict[str, Any]]] = []
        self.rows: list[dict[str, Any]] = [{"hours": 3.0}]

    async def fetch_schema(self, database: str | None = None) -> dict[str, list[dict[str, Any]]]:
        return {"nodes": [{"label": "Flight", "properties": ["flightTime_hours"]}], "relationships": [], "relationshipProperties": []}

    async def run_read_query(
        self, query: str, parameters: dict[str, Any] | None = None, database: str | None = None
    ) -> tuple[list[str], list[dict[str, Any]]]:
        self.read_calls.append((query, parameters or {}))
        return ["hours"], self.rows


class FakeAgentResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeAgent:
    """Simulates an LLM that calls get_graph_schema then query_knowledge_graph."""

    def __init__(self, tools: list[Any]) -> None:
        self.tools = {t.name: t for t in tools}

    async def run(self, question: str) -> FakeAgentResponse:
        await self.tools["get_graph_schema"].func()
        await self.tools["query_knowledge_graph"].func(cypher="MATCH (f:Flight) RETURN sum(f.flightTime_hours) AS hours", parameters={})
        return FakeAgentResponse("The aircraft has flown 3.0 hours.")


class FakeChatClient:
    """Stands in for OpenAIChatClient; returns a FakeAgent wired to the agent's tools."""

    def as_agent(self, *, name: str, instructions: str, tools: list[Any]) -> FakeAgent:
        return FakeAgent(tools)


async def test_agent_ask_records_cypher_and_returns_answer() -> None:
    neo4j = FakeNeo4j()
    agent = KnowledgeGraphAgent(FakeChatClient(), neo4j)  # type: ignore[arg-type]
    result = await agent.ask("How many hours has it flown?")
    assert isinstance(result, AskResult)
    assert result.answer == "The aircraft has flown 3.0 hours."
    assert result.cypher_used == ["MATCH (f:Flight) RETURN sum(f.flightTime_hours) AS hours"]
    assert result.records == [{"hours": 3.0}]
    assert len(neo4j.read_calls) == 1


async def test_agent_query_tool_reports_write_rejection() -> None:
    neo4j = FakeNeo4j()
    agent = KnowledgeGraphAgent(FakeChatClient(), neo4j)  # type: ignore[arg-type]
    result = AskResult(answer="")
    tools = {t.name: t for t in agent._build_tools(result)}
    output = await tools["query_knowledge_graph"].func(cypher="MATCH (n) DETACH DELETE n")
    assert "read-only" in output
    assert result.cypher_used == []
    assert neo4j.read_calls == []


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
