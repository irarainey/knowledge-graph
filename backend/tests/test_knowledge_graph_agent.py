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
from common.telemetry import maf_call_sink, usage_sink


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
    """Async-iterable stand-in for MAF ResponseStream with get_final_response().

    Simulates the agentic flow and the per-turn chat middleware that records each LLM
    call. On the first iteration it: (1) records the agent's *tool-planning* turn into
    ``maf_call_sink``, (2) awaits ``tool_call`` (which populates ``retrieval_sink`` like
    the real tool) and records the *cypher-generation* call into ``usage_sink``. When the
    final response is drained it records the *answer-generation* turn into
    ``maf_call_sink`` — mirroring the three real LLM calls a question makes.
    """

    def __init__(
        self,
        texts: list[str],
        final: FakeAgentResponse,
        *,
        error: Exception | None = None,
        tool_call: Any = None,
        question: str = "",
        instructions: str = "",
        planning_usage: dict[str, Any] | None = None,
        cypher_usage: dict[str, Any] | None = None,
    ) -> None:
        self._texts = texts
        self._final = final
        self._error = error
        self._tool_call = tool_call
        self._question = question
        self._instructions = instructions
        self._planning_usage = planning_usage
        self._cypher_usage = cypher_usage
        self._tool_done = False
        self._answer_recorded = False

    def __aiter__(self) -> FakeResponseStream:
        self._iter = iter(self._texts)
        self._tool_done = False
        return self

    async def __anext__(self) -> FakeUpdate:
        if self._tool_call is not None and not self._tool_done:
            self._tool_done = True
            # The agent's tool-planning turn happens first.
            _record_maf_call("agent_planning", self._planning_usage, self._instructions, self._question)
            # Then the tool runs (cypher generation + graph query).
            await self._tool_call(self._question)
            if self._cypher_usage is not None:
                sink = usage_sink.get()
                if sink is not None:
                    sink.append({**self._cypher_usage, "duration_ms": 1.0, "request": []})
        if self._error is not None:
            raise self._error
        try:
            return FakeUpdate(next(self._iter))
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def get_final_response(self) -> FakeAgentResponse:
        # Draining the final response fires the answer-turn middleware hook.
        if not self._answer_recorded:
            self._answer_recorded = True
            _record_maf_call("answer_generation", self._final.usage_details, self._instructions, self._question)
        return self._final


def _record_maf_call(stage: str, usage: dict[str, Any] | None, instructions: str, question: str) -> None:
    """Append a simulated MAF turn entry to ``maf_call_sink`` (mimics _MafTurnRecorder)."""
    sink = maf_call_sink.get()
    if sink is None:
        return
    tokens = {
        "prompt": (usage or {}).get("input_token_count"),
        "completion": (usage or {}).get("output_token_count"),
        "total": (usage or {}).get("total_token_count"),
    }
    sink.append(
        {
            "stage": stage,
            **tokens,
            "duration_ms": 1.0,
            "request": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": question},
            ],
        }
    )


class FakeMafAgent:
    """Stands in for agent_framework.Agent: invokes the forced tool, returns canned output."""

    def __init__(
        self,
        *,
        text: str = "answer",
        usage_details: dict[str, Any] | None = None,
        stream_texts: list[str] | None = None,
        stream_error: Exception | None = None,
        planning_usage: dict[str, Any] | None = None,
        cypher_usage: dict[str, Any] | None = None,
    ) -> None:
        self._text = text
        self._usage = usage_details
        self._stream_texts = stream_texts or []
        self._stream_error = stream_error
        self._planning_usage = planning_usage
        self._cypher_usage = cypher_usage
        self.prompts: list[str] = []
        self.instructions: str = ""
        # Wired by the test to the agent's retrieval tool to simulate the forced call.
        self.tool_call: Any = None

    def run(self, messages: str, *, stream: bool = True) -> FakeResponseStream:
        self.prompts.append(messages)
        final = FakeAgentResponse(self._text, self._usage)
        return FakeResponseStream(
            self._stream_texts,
            final,
            error=self._stream_error,
            tool_call=self.tool_call,
            question=messages,
            instructions=self.instructions,
            planning_usage=self._planning_usage,
            cypher_usage=self._cypher_usage,
        )


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


_TEST_INSTRUCTIONS = "Answer the user question using the knowledge graph tool."


def _make_stream_agent(retriever: FakeStreamRetriever, maf_agent: FakeMafAgent) -> KnowledgeGraphAgent:
    agent = object.__new__(KnowledgeGraphAgent)
    agent._driver = FakeDriver()  # type: ignore[assignment]
    agent._retriever = retriever  # type: ignore[assignment]
    agent._agent = maf_agent  # type: ignore[assignment]
    agent._llm = SimpleNamespace(model_name="fake-model")  # type: ignore[assignment]
    agent._instructions = _TEST_INSTRUCTIONS
    # Permissive vocabulary so the relevance guardrail passes for "flight" questions.
    agent._vocabulary = frozenset({"flight"})  # type: ignore[assignment]
    # Simulate the agent being forced to call the retrieval tool, and the per-turn chat
    # middleware that records each MAF LLM call.
    maf_agent.tool_call = agent._run_retrieval_tool
    maf_agent.instructions = _TEST_INSTRUCTIONS
    return agent


async def test_ask_emits_metadata_tokens_and_done() -> None:
    result = RetrieverResult(
        items=[RetrieverResultItem(content='{"flights": 6}', metadata={"record": {"flights": 6}})],
        metadata={"cypher": "MATCH (f:Flight) RETURN count(f) AS flights"},
    )
    retriever = FakeStreamRetriever(result=result)
    maf = FakeMafAgent(
        stream_texts=["There ", "are 6 ", "flights."],
        planning_usage={"input_token_count": 10, "output_token_count": 2, "total_token_count": 12},
        cypher_usage={"prompt": 100, "completion": 5, "total": 105},
        usage_details={"input_token_count": 120, "output_token_count": 8, "total_token_count": 128},
    )
    agent = _make_stream_agent(retriever, maf)

    events = [event async for event in agent.ask("How many flights?")]

    # Progress phases are surfaced up front (planning → cypher → answering; the fake
    # retriever bypasses the cypher recorder, so it emits no "querying").
    phases = [event["phase"] for event in events if event["type"] == "progress"]
    assert phases == ["planning", "cypher", "answering"]
    # Metadata is the first non-progress event, before any answer tokens.
    non_progress = [event for event in events if event["type"] != "progress"]
    assert non_progress[0] == {
        "type": "metadata",
        "cypher_used": ["MATCH (f:Flight) RETURN count(f) AS flights"],
        "records": [{"flights": 6}],
    }
    tokens = [event["text"] for event in events if event["type"] == "token"]
    assert "".join(tokens) == "There are 6 flights."
    assert events[-1] == {"type": "done"}
    # The agent's forced tool ran retrieval with the question.
    assert retriever.calls == ["How many flights?"]
    # The MAF agent is handed the raw question; it orchestrates retrieval via the tool.
    assert maf.prompts == ["How many flights?"]
    # A stats event with the per-call token usage precedes done.
    stats = next(event for event in events if event["type"] == "stats")
    assert events.index(stats) == len(events) - 2
    assert stats["model"] == "fake-model"
    # Three real LLM calls: agent tool-planning, cypher generation, answer generation.
    assert stats["llm_calls"] == 3
    assert [call["stage"] for call in stats["calls"]] == ["agent_planning", "cypher_generation", "answer_generation"]
    # Tokens aggregate across all three calls.
    assert stats["tokens"] == {"prompt": 10 + 100 + 120, "completion": 2 + 5 + 8, "total": 12 + 105 + 128}
    planning_call, cypher_call, answer_call = stats["calls"]
    assert (planning_call["prompt"], planning_call["total"]) == (10, 12)
    assert (cypher_call["prompt"], cypher_call["total"]) == (100, 105)
    assert {answer_call["prompt"], answer_call["completion"], answer_call["total"]} == {120, 8, 128}
    assert isinstance(answer_call["duration_ms"], float)
    # The answer-generation request surfaces the messages the model received.
    assert answer_call["request"] == [
        {"role": "system", "content": _TEST_INSTRUCTIONS},
        {"role": "user", "content": "How many flights?"},
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
    # No usage reported by any call — every token field stays None (unknown, not 0).
    maf = FakeMafAgent(
        stream_texts=["ok"],
        usage_details=None,
        planning_usage=None,
        cypher_usage={"prompt": None, "completion": None, "total": None},
    )
    agent = _make_stream_agent(retriever, maf)

    events = [event async for event in agent.ask("how many flights?")]

    stats = next(event for event in events if event["type"] == "stats")
    # All three calls still count even though their token usage is unknown.
    assert stats["llm_calls"] == 3
    assert stats["tokens"] == {"prompt": None, "completion": None, "total": None}
    assert [call["stage"] for call in stats["calls"]] == ["agent_planning", "cypher_generation", "answer_generation"]
    assert events[-1] == {"type": "done"}


async def test_ask_off_topic_question_is_refused() -> None:
    retriever = FakeStreamRetriever(error=AssertionError("retrieval must not run for off-topic"))
    maf = FakeMafAgent(stream_texts=["should not run"])
    agent = _make_stream_agent(retriever, maf)

    events = [event async for event in agent.ask("What is the capital of France?")]

    assert events[0] == {"type": "metadata", "cypher_used": [], "records": []}
    # Off-topic questions short-circuit before the pipeline runs, so no progress events.
    assert not any(event["type"] == "progress" for event in events)
    tokens = [event["text"] for event in events if event["type"] == "token"]
    assert "only answer questions about the aircraft" in "".join(tokens)
    stats = next(event for event in events if event["type"] == "stats")
    assert stats["llm_calls"] == 0
    assert stats["calls"] == []
    assert events[-1] == {"type": "done"}
    # Neither retrieval nor the agent ran.
    assert retriever.calls == []
    assert maf.prompts == []


async def test_ask_degrades_on_retrieval_error() -> None:
    # Retrieval fails inside the tool; the agent still runs and answers from the
    # tool's graceful message, so metadata is empty but generation still happens.
    retriever = FakeStreamRetriever(error=Text2CypherRetrievalError("bad cypher"))
    maf = FakeMafAgent(stream_texts=["I couldn't find that."])
    agent = _make_stream_agent(retriever, maf)

    events = [event async for event in agent.ask("how many flights?")]

    non_progress = [event for event in events if event["type"] != "progress"]
    assert non_progress[0] == {"type": "metadata", "cypher_used": [], "records": []}
    tokens = [event["text"] for event in events if event["type"] == "token"]
    assert "".join(tokens) == "I couldn't find that."
    stats = next(event for event in events if event["type"] == "stats")
    # The agent still made its planning and answer LLM calls (cypher-gen usage is absent
    # because retrieval failed before reporting any).
    assert stats["llm_calls"] == 2
    assert [call["stage"] for call in stats["calls"]] == ["agent_planning", "answer_generation"]
    assert stats["record_count"] == 0
    assert events[-1] == {"type": "done"}
    assert retriever.calls == ["how many flights?"]


async def test_ask_emits_error_event_on_generation_failure() -> None:
    result = RetrieverResult(
        items=[RetrieverResultItem(content="{}", metadata={"record": {"n": 1}})],
        metadata={"cypher": "MATCH (n) RETURN count(n)"},
    )
    retriever = FakeStreamRetriever(result=result)
    maf = FakeMafAgent(stream_texts=["partial"], stream_error=RuntimeError("stream blew up"))
    agent = _make_stream_agent(retriever, maf)

    events = [event async for event in agent.ask("how many flights?")]

    # Metadata still precedes the failure; an error event is emitted, then stats + done.
    non_progress = [event for event in events if event["type"] != "progress"]
    assert non_progress[0]["type"] == "metadata"
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


# ── MAF turn recorder middleware + message serialization ─────────────────────
class FakeContent:
    def __init__(self, type: str, **kwargs: Any) -> None:
        self.type = type
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeMessage:
    def __init__(self, role: str, contents: list[FakeContent], text: str = "") -> None:
        self.role = role
        self.contents = contents
        self.text = text


class FakeChatResponse:
    def __init__(self, messages: list[FakeMessage], usage_details: dict[str, Any] | None) -> None:
        self.messages = messages
        self.usage_details = usage_details


class FakeChatContext:
    """Minimal ChatContext stand-in for exercising a ChatMiddleware in isolation."""

    def __init__(self, messages: list[FakeMessage], *, stream: bool, result: FakeChatResponse) -> None:
        self.messages = messages
        self.stream = stream
        self.result = result
        self.stream_result_hooks: list[Any] = []


def test_serialize_maf_messages_renders_text_calls_and_results() -> None:
    from common.telemetry import serialize_maf_messages

    messages = [
        FakeMessage("user", [FakeContent("text", text="How many flights?")]),
        FakeMessage("assistant", [FakeContent("function_call", name="search_knowledge_graph", arguments='{"question":"q"}')]),
        FakeMessage("tool", [FakeContent("function_result", result='[{"flights": 12}]')]),
    ]

    serialized = serialize_maf_messages(messages)

    assert serialized[0] == {"role": "user", "content": "How many flights?"}
    assert serialized[1]["role"] == "assistant"
    assert "→ call search_knowledge_graph(" in serialized[1]["content"]
    assert serialized[2] == {"role": "tool", "content": '← result: [{"flights": 12}]'}


async def test_maf_turn_recorder_records_planning_and_answer_turns() -> None:
    from agents.knowledge_graph_agent import _MafTurnRecorder
    from common.telemetry import maf_call_sink

    recorder = _MafTurnRecorder("INSTRUCTIONS")
    recorded: list[dict[str, Any]] = []
    token = maf_call_sink.set(recorded)
    try:
        # Turn 1: planning — the response carries a function call.
        planning_response = FakeChatResponse(
            messages=[FakeMessage("assistant", [FakeContent("function_call", name="search_knowledge_graph", arguments="{}")])],
            usage_details={"input_token_count": 10, "output_token_count": 2, "total_token_count": 12},
        )
        ctx1 = FakeChatContext([FakeMessage("user", [FakeContent("text", text="q")])], stream=True, result=planning_response)

        async def _next1() -> None:
            return None

        await recorder.process(ctx1, _next1)  # type: ignore[arg-type]
        # Streaming usage only resolves once the stream finalizes — fire the hook.
        for hook in ctx1.stream_result_hooks:
            hook(planning_response)

        # Turn 2: answer — text only, request includes the tool-result rows.
        answer_response = FakeChatResponse(
            messages=[FakeMessage("assistant", [FakeContent("text", text="12 flights.")], text="12 flights.")],
            usage_details={"input_token_count": 40, "output_token_count": 5, "total_token_count": 45},
        )
        ctx2 = FakeChatContext(
            [
                FakeMessage("user", [FakeContent("text", text="q")]),
                FakeMessage("tool", [FakeContent("function_result", result='[{"flights": 12}]')]),
            ],
            stream=True,
            result=answer_response,
        )

        async def _next2() -> None:
            return None

        await recorder.process(ctx2, _next2)  # type: ignore[arg-type]
        for hook in ctx2.stream_result_hooks:
            hook(answer_response)
    finally:
        maf_call_sink.reset(token)

    assert [call["stage"] for call in recorded] == ["agent_planning", "answer_generation"]
    assert (recorded[0]["prompt"], recorded[0]["total"]) == (10, 12)
    assert (recorded[1]["prompt"], recorded[1]["total"]) == (40, 45)
    # The answer turn's recorded request includes the system instructions and the
    # tool-result rows the model actually saw.
    answer_request = recorded[1]["request"]
    assert answer_request[0] == {"role": "system", "content": "INSTRUCTIONS"}
    assert any("flights" in msg["content"] for msg in answer_request)


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
