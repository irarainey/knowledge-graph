"""Unit tests for the neo4j-graphrag text-to-Cypher agent and the /ask endpoint."""

from __future__ import annotations

import json
from types import SimpleNamespace
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
    _usage_sink,
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


# ── KnowledgeGraphAgent.ask_stream (token streaming) ─────────────────────────
class FakeStreamRetriever:
    """Stands in for Text2CypherRetriever.search (sync)."""

    def __init__(self, *, result: RetrieverResult | None = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def search(self, query_text: str) -> RetrieverResult:
        self.calls.append(query_text)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class FakeChunkStream:
    """Async iterator of OpenAI-style streaming chunks built from plain strings."""

    def __init__(self, texts: list[str], usage: Any = None) -> None:
        self._texts = texts
        self._usage = usage
        self.closed = False

    def __aiter__(self) -> FakeChunkStream:
        self._iter = iter(self._texts)
        self._usage_sent = False
        return self

    async def __anext__(self) -> Any:
        try:
            text = next(self._iter)
        except StopIteration as exc:
            # After the text chunks, emit a final usage-only chunk (empty choices),
            # mirroring OpenAI's stream_options={"include_usage": True} behaviour.
            if self._usage is not None and not self._usage_sent:
                self._usage_sent = True
                return SimpleNamespace(choices=[], usage=self._usage)
            raise StopAsyncIteration from exc
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])

    async def aclose(self) -> None:
        self.closed = True


class FakeCompletions:
    def __init__(self, texts: list[str], usage: Any = None) -> None:
        self._texts = texts
        self._usage = usage
        self.last_kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> FakeChunkStream:
        self.last_kwargs = kwargs
        return FakeChunkStream(self._texts, self._usage)


class FakeStreamLLM:
    """Minimal stand-in for OpenAILLM exposing the bits _stream_answer uses."""

    def __init__(self, texts: list[str], usage: Any = None) -> None:
        self.model_name = "fake-model"
        self.model_params: dict[str, Any] = {}
        self.completions = FakeCompletions(texts, usage)
        self.async_client = SimpleNamespace(chat=SimpleNamespace(completions=self.completions))

    def get_messages(self, input: str, message_history: Any = None, system_instruction: str | None = None) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": input})
        return messages


class FakePromptTemplate:
    system_instructions = "Answer the user question using the provided context."

    def format(self, query_text: str, context: str, examples: str) -> str:
        return f"Q: {query_text}\nCTX: {context}"


def _make_stream_agent(retriever: FakeStreamRetriever, llm: FakeStreamLLM) -> KnowledgeGraphAgent:
    agent = object.__new__(KnowledgeGraphAgent)
    agent._driver = FakeDriver()  # type: ignore[assignment]
    agent._retriever = retriever  # type: ignore[assignment]
    agent._llm = llm  # type: ignore[assignment]
    agent._prompt_template = FakePromptTemplate()  # type: ignore[assignment]
    return agent


async def test_ask_stream_emits_metadata_tokens_and_done() -> None:
    result = RetrieverResult(
        items=[RetrieverResultItem(content='{"flights": 6}', metadata={"record": {"flights": 6}})],
        metadata={"cypher": "MATCH (f:Flight) RETURN count(f) AS flights"},
    )
    retriever = FakeStreamRetriever(result=result)
    llm = FakeStreamLLM(
        ["There ", "are 6 ", "flights."],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=8, total_tokens=128),
    )
    agent = _make_stream_agent(retriever, llm)

    events = [event async for event in agent.ask_stream("How many flights?")]

    assert events[0] == {
        "type": "metadata",
        "cypher_used": ["MATCH (f:Flight) RETURN count(f) AS flights"],
        "records": [{"flights": 6}],
    }
    tokens = [event["text"] for event in events if event["type"] == "token"]
    assert "".join(tokens) == "There are 6 flights."
    assert events[-1] == {"type": "done"}
    assert retriever.calls == ["How many flights?"]
    # The native streaming call must request streaming, pass the model through, and
    # request token usage on the final chunk.
    assert llm.completions.last_kwargs is not None
    assert llm.completions.last_kwargs["stream"] is True
    assert llm.completions.last_kwargs["model"] == "fake-model"
    assert llm.completions.last_kwargs["stream_options"] == {"include_usage": True}
    # A stats event with the answer-generation token usage precedes done.
    stats = next(event for event in events if event["type"] == "stats")
    assert events.index(stats) == len(events) - 2
    assert stats["model"] == "fake-model"
    assert stats["llm_calls"] == 1
    assert stats["tokens"] == {"prompt": 120, "completion": 8, "total": 128}
    assert len(stats["calls"]) == 1
    answer_call = stats["calls"][0]
    assert answer_call["stage"] == "answer_generation"
    assert {answer_call["prompt"], answer_call["completion"], answer_call["total"]} == {120, 8, 128}
    assert isinstance(answer_call["duration_ms"], float)
    # The answer-generation request (the messages sent to the LLM) is surfaced.
    assert answer_call["request"] == [
        {"role": "system", "content": "Answer the user question using the provided context."},
        {"role": "user", "content": 'Q: How many flights?\nCTX: {"flights": 6}'},
    ]
    assert stats["cypher_count"] == 1
    assert stats["record_count"] == 1
    assert set(stats["durations_ms"]) == {"retrieval", "graph_query", "generation", "total"}


async def test_ask_stream_degrades_on_retrieval_error() -> None:
    retriever = FakeStreamRetriever(error=Text2CypherRetrievalError("bad cypher"))
    llm = FakeStreamLLM([])
    agent = _make_stream_agent(retriever, llm)

    events = [event async for event in agent.ask_stream("nonsense")]

    assert events[0] == {"type": "metadata", "cypher_used": [], "records": []}
    assert any(event["type"] == "token" and "couldn't" in event["text"].lower() for event in events)
    stats = next(event for event in events if event["type"] == "stats")
    assert stats["llm_calls"] == 0
    assert stats["calls"] == []
    assert stats["tokens"] == {"prompt": None, "completion": None, "total": None}
    assert events[-1] == {"type": "done"}


def test_install_usage_recorder_captures_invoke_usage() -> None:
    agent = object.__new__(KnowledgeGraphAgent)
    llm = SimpleNamespace(
        invoke=lambda *a, **k: SimpleNamespace(
            content="MATCH (n) RETURN n",
            usage=SimpleNamespace(request_tokens=40, response_tokens=12, total_tokens=52),
        )
    )
    agent._llm = llm  # type: ignore[assignment]
    agent._install_usage_recorder()

    sink: list[dict[str, Any]] = []
    token = _usage_sink.set(sink)
    try:
        agent._llm.invoke("the cypher prompt")
    finally:
        _usage_sink.reset(token)

    assert len(sink) == 1
    assert sink[0]["prompt"] == 40
    assert sink[0]["completion"] == 12
    assert sink[0]["total"] == 52
    assert isinstance(sink[0]["duration_ms"], float)
    # The cypher-generation request (the prompt sent to the LLM) is captured.
    assert sink[0]["request"] == [{"role": "user", "content": "the cypher prompt"}]
    # Re-installing must not double-wrap.
    assert getattr(agent._llm, "_kg_usage_wrapped", False) is True


# ── /ask endpoint ────────────────────────────────────────────────────────────
class FakeKGAgent:
    async def ask(self, question: str) -> AskResult:
        return AskResult(answer="42", cypher_used=["MATCH (n) RETURN count(n)"], records=[{"count": 1}])

    async def ask_stream(self, question: str) -> Any:
        yield {"type": "metadata", "cypher_used": ["MATCH (n) RETURN count(n)"], "records": [{"count": 1}]}
        yield {"type": "token", "text": "42"}
        yield {
            "type": "stats",
            "model": "fake-model",
            "llm_calls": 2,
            "tokens": {"prompt": 100, "completion": 10, "total": 110},
            "calls": [],
            "durations_ms": {"retrieval": 1.0, "graph_query": 0.5, "generation": 2.0, "total": 3.0},
            "cypher_count": 1,
            "record_count": 1,
        }
        yield {"type": "done"}


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


async def test_ask_stream_endpoint_streams_ndjson() -> None:
    api.app.dependency_overrides[api.get_agent] = lambda: FakeKGAgent()
    transport = ASGITransport(app=api.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/ask/stream", json={"question": "How many nodes?"})
    finally:
        api.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines() if line]
    assert events[0]["type"] == "metadata"
    assert any(event["type"] == "token" and event["text"] == "42" for event in events)
    assert events[-1] == {"type": "done"}


async def test_ask_stream_endpoint_503_when_agent_unconfigured() -> None:
    api.app.state.agent = None
    transport = ASGITransport(app=api.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ask/stream", json={"question": "anything"})
    assert response.status_code == 503
