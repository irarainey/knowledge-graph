"""Unit tests for the neo4j-graphrag text-to-Cypher agent and the /ask endpoint."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from neo4j_graphrag.exceptions import Text2CypherRetrievalError
from neo4j_graphrag.llm import AzureOpenAILLM, OpenAILLM
from neo4j_graphrag.types import RetrieverResult, RetrieverResultItem

import app
from agents.knowledge_graph_agent import AskResult, KnowledgeGraphAgent
from common.azure_openai import AzureOpenAISettings, build_llm
from common.graph_schema import fetch_schema_text
from common.retrieval import record_to_item
from common.telemetry import usage_sink


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


# ── record_to_item ──────────────────────────────────────────────────────────
def test_record_to_item_serialises_and_keeps_record() -> None:
    item = record_to_item(FakeRecord(flights=6, hours=3.0))
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


async def _value_coro(value: Any) -> Any:
    return value


async def _raise_coro(exc: Exception) -> Any:
    raise exc


class FakeUpdate:
    """Stands in for AgentResponseUpdate (streaming chunk)."""

    def __init__(self, text: str) -> None:
        self.text = text


class FakeAgentResponse:
    """Stands in for AgentResponse: exposes .text and .usage_details."""

    def __init__(self, text: str, usage_details: dict[str, Any] | None = None) -> None:
        self.text = text
        self.usage_details = usage_details


class FakeResponseStream:
    """Async-iterable stand-in for MAF ResponseStream with get_final_response()."""

    def __init__(self, texts: list[str], final: FakeAgentResponse, *, error: Exception | None = None) -> None:
        self._texts = texts
        self._final = final
        self._error = error

    def __aiter__(self) -> FakeResponseStream:
        self._iter = iter(self._texts)
        return self

    async def __anext__(self) -> FakeUpdate:
        if self._error is not None:
            raise self._error
        try:
            return FakeUpdate(next(self._iter))
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def get_final_response(self) -> FakeAgentResponse:
        return self._final


class FakeMafAgent:
    """Stands in for agent_framework.Agent: records prompts, returns canned output."""

    def __init__(
        self,
        *,
        text: str = "answer",
        usage_details: dict[str, Any] | None = None,
        stream_texts: list[str] | None = None,
        error: Exception | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        self._text = text
        self._usage = usage_details
        self._stream_texts = stream_texts or []
        self._error = error
        self._stream_error = stream_error
        self.prompts: list[str] = []

    def run(self, messages: str, *, stream: bool = False) -> Any:
        self.prompts.append(messages)
        final = FakeAgentResponse(self._text, self._usage)
        if stream:
            return FakeResponseStream(self._stream_texts, final, error=self._stream_error)
        if self._error is not None:
            return _raise_coro(self._error)
        return _value_coro(final)


class FakePromptTemplate:
    system_instructions = "Answer the user question using the provided context."

    def format(self, query_text: str, context: str, examples: str) -> str:
        return f"Q: {query_text}\nCTX: {context}"


def _make_ask_agent(retriever: Any, maf_agent: FakeMafAgent) -> tuple[KnowledgeGraphAgent, FakeDriver]:
    agent = object.__new__(KnowledgeGraphAgent)
    driver = FakeDriver()
    agent._driver = driver  # type: ignore[assignment]
    agent._retriever = retriever  # type: ignore[assignment]
    agent._agent = maf_agent  # type: ignore[assignment]
    agent._prompt_template = FakePromptTemplate()  # type: ignore[assignment]
    agent._instructions = FakePromptTemplate.system_instructions  # type: ignore[assignment]
    return agent, driver


async def test_ask_extracts_answer_cypher_and_records() -> None:
    result = RetrieverResult(
        items=[RetrieverResultItem(content="{}", metadata={"record": {"flights": 6}})],
        metadata={"cypher": "MATCH (f:Flight) RETURN count(f) AS flights"},
    )
    retriever = FakeStreamRetriever(result=result)
    maf = FakeMafAgent(text="There are 6 flights.")
    agent, _ = _make_ask_agent(retriever, maf)
    ask_result = await agent.ask("How many flights?")
    assert isinstance(ask_result, AskResult)
    assert ask_result.answer == "There are 6 flights."
    assert ask_result.cypher_used == ["MATCH (f:Flight) RETURN count(f) AS flights"]
    assert ask_result.records == [{"flights": 6}]
    assert retriever.calls == ["How many flights?"]
    # The MAF agent is handed the formatted prompt built from the retrieved rows.
    assert maf.prompts == ["Q: How many flights?\nCTX: {}"]


async def test_ask_degrades_gracefully_on_retrieval_error() -> None:
    retriever = FakeStreamRetriever(error=Text2CypherRetrievalError("bad cypher"))
    maf = FakeMafAgent()
    agent, _ = _make_ask_agent(retriever, maf)
    ask_result = await agent.ask("nonsense")
    assert ask_result.cypher_used == []
    assert ask_result.records == []
    assert "couldn't" in ask_result.answer.lower()
    assert maf.prompts == []  # generation never attempted when retrieval fails


async def test_ask_degrades_gracefully_on_unexpected_error() -> None:
    retriever = FakeStreamRetriever(error=RuntimeError("driver down"))
    maf = FakeMafAgent()
    agent, _ = _make_ask_agent(retriever, maf)
    ask_result = await agent.ask("anything")
    assert ask_result.cypher_used == []
    assert "couldn't" in ask_result.answer.lower()


async def test_ask_degrades_gracefully_on_generation_error() -> None:
    result = RetrieverResult(
        items=[RetrieverResultItem(content="{}", metadata={"record": {"flights": 6}})],
        metadata={"cypher": "MATCH (f:Flight) RETURN count(f) AS flights"},
    )
    retriever = FakeStreamRetriever(result=result)
    maf = FakeMafAgent(error=RuntimeError("LLM down"))
    agent, _ = _make_ask_agent(retriever, maf)
    ask_result = await agent.ask("How many flights?")
    # The Cypher/records the retriever found are still returned even if the answer fails.
    assert ask_result.cypher_used == ["MATCH (f:Flight) RETURN count(f) AS flights"]
    assert ask_result.records == [{"flights": 6}]
    assert "couldn't" in ask_result.answer.lower()


def test_close_closes_driver() -> None:
    agent, driver = _make_ask_agent(FakeStreamRetriever(error=RuntimeError()), FakeMafAgent())
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


def _make_stream_agent(retriever: FakeStreamRetriever, maf_agent: FakeMafAgent) -> KnowledgeGraphAgent:
    agent = object.__new__(KnowledgeGraphAgent)
    agent._driver = FakeDriver()  # type: ignore[assignment]
    agent._retriever = retriever  # type: ignore[assignment]
    agent._agent = maf_agent  # type: ignore[assignment]
    agent._llm = SimpleNamespace(model_name="fake-model")  # type: ignore[assignment]
    agent._prompt_template = FakePromptTemplate()  # type: ignore[assignment]
    agent._instructions = FakePromptTemplate.system_instructions  # type: ignore[assignment]
    return agent


async def test_ask_stream_emits_metadata_tokens_and_done() -> None:
    result = RetrieverResult(
        items=[RetrieverResultItem(content='{"flights": 6}', metadata={"record": {"flights": 6}})],
        metadata={"cypher": "MATCH (f:Flight) RETURN count(f) AS flights"},
    )
    retriever = FakeStreamRetriever(result=result)
    maf = FakeMafAgent(
        stream_texts=["There ", "are 6 ", "flights."],
        usage_details={"input_token_count": 120, "output_token_count": 8, "total_token_count": 128},
    )
    agent = _make_stream_agent(retriever, maf)

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
    # The MAF agent is handed the formatted prompt built from the retrieved rows.
    assert maf.prompts == ['Q: How many flights?\nCTX: {"flights": 6}']
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
    # The answer-generation request (the messages sent to the agent) is surfaced.
    assert answer_call["request"] == [
        {"role": "system", "content": "Answer the user question using the provided context."},
        {"role": "user", "content": 'Q: How many flights?\nCTX: {"flights": 6}'},
    ]
    assert stats["cypher_count"] == 1
    assert stats["record_count"] == 1
    assert set(stats["durations_ms"]) == {"retrieval", "graph_query", "generation", "total"}


async def test_ask_stream_handles_missing_usage() -> None:
    result = RetrieverResult(
        items=[RetrieverResultItem(content="{}", metadata={"record": {"n": 1}})],
        metadata={"cypher": "MATCH (n) RETURN count(n)"},
    )
    retriever = FakeStreamRetriever(result=result)
    # No usage_details — the endpoint reported no token usage for the stream.
    maf = FakeMafAgent(stream_texts=["ok"], usage_details=None)
    agent = _make_stream_agent(retriever, maf)

    events = [event async for event in agent.ask_stream("anything")]

    stats = next(event for event in events if event["type"] == "stats")
    # The answer call still counts even though tokens are unknown (None, not 0).
    assert stats["llm_calls"] == 1
    assert stats["tokens"] == {"prompt": None, "completion": None, "total": None}
    assert stats["calls"][0]["stage"] == "answer_generation"
    assert events[-1] == {"type": "done"}


async def test_ask_stream_degrades_on_retrieval_error() -> None:
    retriever = FakeStreamRetriever(error=Text2CypherRetrievalError("bad cypher"))
    maf = FakeMafAgent()
    agent = _make_stream_agent(retriever, maf)

    events = [event async for event in agent.ask_stream("nonsense")]

    assert events[0] == {"type": "metadata", "cypher_used": [], "records": []}
    assert any(event["type"] == "token" and "couldn't" in event["text"].lower() for event in events)
    stats = next(event for event in events if event["type"] == "stats")
    assert stats["llm_calls"] == 0
    assert stats["calls"] == []
    assert stats["tokens"] == {"prompt": None, "completion": None, "total": None}
    assert events[-1] == {"type": "done"}
    assert maf.prompts == []  # generation never attempted when retrieval fails


async def test_ask_stream_emits_error_event_on_generation_failure() -> None:
    result = RetrieverResult(
        items=[RetrieverResultItem(content="{}", metadata={"record": {"n": 1}})],
        metadata={"cypher": "MATCH (n) RETURN count(n)"},
    )
    retriever = FakeStreamRetriever(result=result)
    maf = FakeMafAgent(stream_texts=["partial"], stream_error=RuntimeError("stream blew up"))
    agent = _make_stream_agent(retriever, maf)

    events = [event async for event in agent.ask_stream("anything")]

    # Metadata still precedes the failure; an error event is emitted, then stats + done.
    assert events[0]["type"] == "metadata"
    assert any(event["type"] == "error" for event in events)
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
    token = usage_sink.set(sink)
    try:
        agent._llm.invoke("the cypher prompt")
    finally:
        usage_sink.reset(token)

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
    app.app.dependency_overrides[app._get_agent] = lambda: FakeKGAgent()
    transport = ASGITransport(app=app.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/ask", json={"question": "How many nodes?"})
    finally:
        app.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"answer": "42", "cypher_used": ["MATCH (n) RETURN count(n)"], "records": [{"count": 1}]}


async def test_ask_endpoint_503_when_agent_unconfigured() -> None:
    # No dependency override and no app.state.agent -> get_agent raises 503.
    app.app.state.agent = None
    transport = ASGITransport(app=app.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ask", json={"question": "anything"})
    assert response.status_code == 503


async def test_ask_endpoint_rejects_empty_question() -> None:
    app.app.dependency_overrides[app._get_agent] = lambda: FakeKGAgent()
    transport = ASGITransport(app=app.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/ask", json={"question": ""})
    finally:
        app.app.dependency_overrides.clear()
    assert response.status_code == 422


async def test_ask_stream_endpoint_streams_ndjson() -> None:
    app.app.dependency_overrides[app._get_agent] = lambda: FakeKGAgent()
    transport = ASGITransport(app=app.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/ask/stream", json={"question": "How many nodes?"})
    finally:
        app.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines() if line]
    assert events[0]["type"] == "metadata"
    assert any(event["type"] == "token" and event["text"] == "42" for event in events)
    assert events[-1] == {"type": "done"}


async def test_ask_stream_endpoint_503_when_agent_unconfigured() -> None:
    app.app.state.agent = None
    transport = ASGITransport(app=app.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ask/stream", json={"question": "anything"})
    assert response.status_code == 503
