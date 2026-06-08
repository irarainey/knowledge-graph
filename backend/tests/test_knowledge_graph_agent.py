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
from agents.knowledge_graph_agent import KnowledgeGraphAgent
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


def test_build_llm_omits_temperature_by_default() -> None:
    settings = AzureOpenAISettings("https://res.openai.azure.com/openai/v1", "key", "gpt-5.4", "2024-10-21")
    llm = build_llm(settings)
    assert llm.model_params == {}


def test_build_llm_pins_temperature_when_set() -> None:
    settings = AzureOpenAISettings("https://res.openai.azure.com/openai/v1", "key", "gpt-5.4", "2024-10-21", temperature=0)
    llm = build_llm(settings)
    assert llm.model_params == {"temperature": 0}


# ── Test fakes ───────────────────────────────────────────────────────────────
class FakeDriver:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


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
        stream_error: Exception | None = None,
    ) -> None:
        self._text = text
        self._usage = usage_details
        self._stream_texts = stream_texts or []
        self._stream_error = stream_error
        self.prompts: list[str] = []

    def run(self, messages: str, *, stream: bool = True) -> FakeResponseStream:
        self.prompts.append(messages)
        final = FakeAgentResponse(self._text, self._usage)
        return FakeResponseStream(self._stream_texts, final, error=self._stream_error)


class FakePromptTemplate:
    system_instructions = "Answer the user question using the provided context."

    def format(self, query_text: str, context: str, examples: str) -> str:
        return f"Q: {query_text}\nCTX: {context}"


def test_close_closes_driver() -> None:
    agent = object.__new__(KnowledgeGraphAgent)
    driver = FakeDriver()
    agent._driver = driver  # type: ignore[assignment]
    agent.close()
    assert driver.closed is True


# ── KnowledgeGraphAgent.ask (token streaming) ─────────────────────────
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


async def test_ask_emits_metadata_tokens_and_done() -> None:
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

    events = [event async for event in agent.ask("How many flights?")]

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


async def test_ask_handles_missing_usage() -> None:
    result = RetrieverResult(
        items=[RetrieverResultItem(content="{}", metadata={"record": {"n": 1}})],
        metadata={"cypher": "MATCH (n) RETURN count(n)"},
    )
    retriever = FakeStreamRetriever(result=result)
    # No usage_details — the endpoint reported no token usage for the stream.
    maf = FakeMafAgent(stream_texts=["ok"], usage_details=None)
    agent = _make_stream_agent(retriever, maf)

    events = [event async for event in agent.ask("anything")]

    stats = next(event for event in events if event["type"] == "stats")
    # The answer call still counts even though tokens are unknown (None, not 0).
    assert stats["llm_calls"] == 1
    assert stats["tokens"] == {"prompt": None, "completion": None, "total": None}
    assert stats["calls"][0]["stage"] == "answer_generation"
    assert events[-1] == {"type": "done"}


async def test_ask_degrades_on_retrieval_error() -> None:
    retriever = FakeStreamRetriever(error=Text2CypherRetrievalError("bad cypher"))
    maf = FakeMafAgent()
    agent = _make_stream_agent(retriever, maf)

    events = [event async for event in agent.ask("nonsense")]

    assert events[0] == {"type": "metadata", "cypher_used": [], "records": []}
    assert any(event["type"] == "token" and "couldn't" in event["text"].lower() for event in events)
    stats = next(event for event in events if event["type"] == "stats")
    assert stats["llm_calls"] == 0
    assert stats["calls"] == []
    assert stats["tokens"] == {"prompt": None, "completion": None, "total": None}
    assert events[-1] == {"type": "done"}
    assert maf.prompts == []  # generation never attempted when retrieval fails


async def test_ask_emits_error_event_on_generation_failure() -> None:
    result = RetrieverResult(
        items=[RetrieverResultItem(content="{}", metadata={"record": {"n": 1}})],
        metadata={"cypher": "MATCH (n) RETURN count(n)"},
    )
    retriever = FakeStreamRetriever(result=result)
    maf = FakeMafAgent(stream_texts=["partial"], stream_error=RuntimeError("stream blew up"))
    agent = _make_stream_agent(retriever, maf)

    events = [event async for event in agent.ask("anything")]

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


def test_install_usage_recorder_emits_cypher_generation_span(monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import agents.knowledge_graph_agent as kga

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(kga, "tracer", provider.get_tracer("test"))

    agent = object.__new__(KnowledgeGraphAgent)
    agent._llm = SimpleNamespace(  # type: ignore[assignment]
        model_name="fake-model",
        invoke=lambda *a, **k: SimpleNamespace(
            content="MATCH (n) RETURN n",
            usage=SimpleNamespace(request_tokens=40, response_tokens=12, total_tokens=52),
        ),
    )
    agent._install_usage_recorder()
    agent._llm.invoke("the cypher prompt")

    spans = [s for s in exporter.get_finished_spans() if s.name == "chat fake-model"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.request.model"] == "fake-model"
    assert attrs["gen_ai.usage.input_tokens"] == 40
    assert attrs["gen_ai.usage.output_tokens"] == 12


# ── /ask endpoint ─────────────────────────────────────────────────────
class FakeKGAgent:
    async def ask(self, question: str) -> Any:
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


async def test_ask_endpoint_rejects_empty_question() -> None:
    app.app.dependency_overrides[app._get_agent] = lambda: FakeKGAgent()
    transport = ASGITransport(app=app.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/ask", json={"question": ""})
    finally:
        app.app.dependency_overrides.clear()
    assert response.status_code == 422


async def test_ask_endpoint_streams_ndjson() -> None:
    app.app.dependency_overrides[app._get_agent] = lambda: FakeKGAgent()
    transport = ASGITransport(app=app.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/ask", json={"question": "How many nodes?"})
    finally:
        app.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines() if line]
    assert events[0]["type"] == "metadata"
    assert any(event["type"] == "token" and event["text"] == "42" for event in events)
    assert events[-1] == {"type": "done"}


async def test_ask_endpoint_503_when_agent_unconfigured() -> None:
    app.app.state.agent = None
    transport = ASGITransport(app=app.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ask", json={"question": "anything"})
    assert response.status_code == 503
