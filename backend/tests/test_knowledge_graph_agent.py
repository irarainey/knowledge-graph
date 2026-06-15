"""Unit tests for the structured-intent knowledge-graph agent and the /ask endpoint."""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import app
from agents.knowledge_graph_agent import KnowledgeGraphAgent
from authz import Aggregate, AggregateFunc, PolicyStore, QueryIntent
from common.azure_openai import AzureOpenAISettings
from common.graph_schema import fetch_schema_text
from common.ontology import OntologyMeta
from common.telemetry import maf_call_sink, retrieval_sink
from documents import DocumentExcerpt


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
    ``maf_call_sink``, then (2) awaits ``tool_call`` (which validates the typed intent and
    populates ``retrieval_sink`` like the real tool). When the final response is drained it
    records the *answer-generation* turn into ``maf_call_sink`` — mirroring the **two** real
    LLM calls a structured-intent question makes (no cypher-generation LLM call).
    """

    def __init__(
        self,
        texts: list[str],
        final: FakeAgentResponse,
        *,
        error: Exception | None = None,
        tool_call: Any = None,
        intent: QueryIntent | None = None,
        intents: list[QueryIntent] | None = None,
        question: str = "",
        instructions: str = "",
        planning_usage: dict[str, Any] | None = None,
    ) -> None:
        self._texts = texts
        self._final = final
        self._error = error
        self._tool_call = tool_call
        self._intents = intents if intents is not None else ([intent] if intent is not None else [])
        self._question = question
        self._instructions = instructions
        self._planning_usage = planning_usage
        self._tool_done = False
        self._answer_recorded = False

    def __aiter__(self) -> FakeResponseStream:
        self._iter = iter(self._texts)
        self._tool_done = False
        return self

    async def __anext__(self) -> FakeUpdate:
        if self._tool_call is not None and not self._tool_done:
            self._tool_done = True
            # The agent's tool-planning turn happens first (it emits the typed intent).
            _record_maf_call("agent_planning", self._planning_usage, self._instructions, self._question)
            # Then the tool runs (possibly more than once): validate each intent against
            # policy, build + run the query. Multiple calls mimic the agent issuing follow-up
            # retrievals (e.g. resolving aerodrome codes to names) before answering.
            for intent in self._intents:
                await self._tool_call(intent)
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
        intent: QueryIntent | None = None,
        intents: list[QueryIntent] | None = None,
    ) -> None:
        self._text = text
        self._usage = usage_details
        self._stream_texts = stream_texts or []
        self._stream_error = stream_error
        self._planning_usage = planning_usage
        self._intent = intent
        self._intents = intents
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
            intent=self._intent,
            intents=self._intents,
            question=messages,
            instructions=self.instructions,
            planning_usage=self._planning_usage,
        )


def test_close_closes_driver() -> None:
    agent = object.__new__(KnowledgeGraphAgent)
    driver = FakeDriver()
    agent._driver = driver  # type: ignore[assignment]
    agent.close()
    assert driver.closed is True


# ── KnowledgeGraphAgent.ask (structured-intent flow) ──────────────────────────
class FakeQueryResult:
    """Stands in for neo4j EagerResult: exposes ``.records``."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records


class FakeQueryDriver:
    """Stands in for the agent's Neo4j driver: returns canned rows or raises on execute_query."""

    def __init__(self, *, records: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self._records = records or []
        self._error = error
        self.closed = False
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def execute_query(
        self, query: str, *, parameters_: dict[str, Any] | None = None, database_: str | None = None, routing_: Any = None
    ) -> FakeQueryResult:
        self.queries.append((query, parameters_ or {}))
        if self._error is not None:
            raise self._error
        return FakeQueryResult(list(self._records))

    def close(self) -> None:
        self.closed = True


_TEST_INSTRUCTIONS = "Answer the user question using the knowledge graph tool."


def _make_stream_agent(
    maf_agent: FakeMafAgent,
    *,
    records: list[dict[str, Any]] | None = None,
    query_error: Exception | None = None,
    user: str = "restricted_ops",
) -> tuple[KnowledgeGraphAgent, FakeQueryDriver, Any]:
    agent = object.__new__(KnowledgeGraphAgent)
    driver = FakeQueryDriver(records=records, error=query_error)
    agent._driver = driver  # type: ignore[assignment]
    agent._policy = PolicyStore.load()  # type: ignore[assignment]
    agent._database = "graph"  # type: ignore[assignment]
    agent._model = "fake-model"  # type: ignore[assignment]
    agent._schema_fingerprint = "testschemafp"  # type: ignore[assignment]
    agent._ontology = OntologyMeta(version="test-ontology")  # type: ignore[assignment]
    # Pre-load the aerodrome name cache so retrieval does not issue an extra lookup query.
    agent._aerodrome_names = {"EGGD": "Bristol", "EGBP": "Cotswold Airport"}  # type: ignore[assignment]
    # Permissive vocabulary so the relevance guardrail passes for "flight" questions.
    agent._vocabulary = frozenset({"flight"})  # type: ignore[assignment]

    # ask() builds the per-request agent via _build_maf_agent(principal); return the fake and
    # bind its forced tool call to the agent's real _run_query_tool (with the principal).
    def build(principal: Any, as_of: Any = None) -> FakeMafAgent:
        maf_agent.tool_call = lambda intent: agent._run_query_tool(principal, intent, as_of)
        maf_agent.instructions = _TEST_INSTRUCTIONS
        return maf_agent

    agent._build_maf_agent = build  # type: ignore[assignment,method-assign]
    principal = agent._policy.resolve_principal(user)
    return agent, driver, principal


_COUNT_INTENT = QueryIntent(entity="Flight", aggregate=Aggregate(func=AggregateFunc.COUNT))


async def test_ask_emits_metadata_tokens_and_done() -> None:
    maf = FakeMafAgent(
        stream_texts=["There ", "are 6 ", "flights."],
        planning_usage={"input_token_count": 10, "output_token_count": 2, "total_token_count": 12},
        usage_details={"input_token_count": 120, "output_token_count": 8, "total_token_count": 128},
        intent=_COUNT_INTENT,
    )
    agent, driver, principal = _make_stream_agent(maf, records=[{"flightResult": 6}])

    events = [event async for event in agent.ask("How many flights?", principal=principal)]

    # Progress phases surface the real pipeline boundaries: planning → build → query → answer.
    phases = [event["phase"] for event in events if event["type"] == "progress"]
    assert phases == ["planning", "cypher", "querying", "answering"]
    # Metadata is the first non-progress event, before any answer tokens.
    non_progress = [event for event in events if event["type"] != "progress"]
    metadata = non_progress[0]
    assert metadata["type"] == "metadata"
    # The Cypher is built deterministically by the backend (count aggregate over Flight).
    assert metadata["cypher_used"] and "count(n)" in metadata["cypher_used"][0]
    assert metadata["records"] == [{"flightResult": 6}]
    # The agent invoked the graph-query tool, surfaced for tool-selection evaluation.
    assert metadata["tools_used"] == ["query_knowledge_graph"]
    # The structured intent the model emitted is surfaced for intent-selection evaluation.
    assert metadata["intents_used"] == [
        {"entity": "Flight", "fields": [], "filters": [], "aggregate": {"func": "count", "field": None}, "limit": None}
    ]
    stats = next(event for event in events if event["type"] == "stats")
    assert stats["tools_used"] == ["query_knowledge_graph"]
    tokens = [event["text"] for event in events if event["type"] == "token"]
    assert "".join(tokens) == "There are 6 flights."
    assert events[-1] == {"type": "done"}
    # The deterministic query ran against the driver with parameters (clearance filter).
    assert len(driver.queries) == 1
    assert "__authz_classifications" in driver.queries[0][1]
    # The MAF agent is handed the raw question; it orchestrates retrieval via the tool.
    assert maf.prompts == ["How many flights?"]
    # A stats event with the per-call token usage precedes done.
    stats = next(event for event in events if event["type"] == "stats")
    assert events.index(stats) == len(events) - 2
    assert stats["model"] == "fake-model"
    # Two real LLM calls: agent tool-planning and answer generation (no cypher-gen call).
    assert stats["llm_calls"] == 2
    assert [call["stage"] for call in stats["calls"]] == ["agent_planning", "answer_generation"]
    # Tokens aggregate across both calls.
    assert stats["tokens"] == {"prompt": 10 + 120, "completion": 2 + 8, "total": 12 + 128}
    # The versioning block reflects the request (default current mode) and ontology version.
    assert stats["versioning"] == {
        "mode": "current",
        "as_of": None,
        "temporal_filter_applied": False,
        "ontology_version": "test-ontology",
    }
    planning_call, answer_call = stats["calls"]
    assert (planning_call["prompt"], planning_call["total"]) == (10, 12)
    assert {answer_call["prompt"], answer_call["completion"], answer_call["total"]} == {120, 8, 128}
    assert isinstance(answer_call["duration_ms"], float)
    assert stats["cypher_count"] == 1
    assert stats["record_count"] == 1
    assert set(stats["durations_ms"]) == {"retrieval", "graph_query", "generation", "total"}
    # An audit record is embedded for every answered request.
    audit = stats["audit"]
    assert audit["outcome"] == "answered"
    assert audit["question"] == "How many flights?"
    assert audit["schemaFingerprint"] == "testschemafp"
    assert audit["denied"] == []


async def test_ask_final_metadata_aggregates_all_retrievals() -> None:
    # When the agent issues several tool calls in one turn (e.g. first listing a flight's
    # destination aerodrome codes, then resolving each code to a name), the final metadata
    # event must carry EVERY query and row so the debug panel shows the complete picture —
    # not just the first retrieval.
    flight_intent = QueryIntent(entity="Flight", fields=["destinationAerodrome"])
    aerodrome_intent = QueryIntent(entity="Aerodrome", fields=["name", "icao"])
    maf = FakeMafAgent(stream_texts=["Bristol (EGGD)."], intents=[flight_intent, aerodrome_intent])
    agent, driver, principal = _make_stream_agent(maf, records=[{"row": 1}])

    events = [event async for event in agent.ask("which aerodromes did each flight reach?", principal=principal)]

    metadata_events = [event for event in events if event["type"] == "metadata"]
    # The final metadata event is authoritative: both queries and the rows from both runs.
    final_metadata = metadata_events[-1]
    assert len(final_metadata["cypher_used"]) == 2
    assert len(final_metadata["records"]) == 2
    assert len(driver.queries) == 2
    # Stats agree with the aggregated metadata.
    stats = next(event for event in events if event["type"] == "stats")
    assert stats["cypher_count"] == 2
    assert stats["record_count"] == 2
    assert events[-1] == {"type": "done"}


async def test_ask_handles_missing_usage() -> None:
    # No usage reported by any call — every token field stays None (unknown, not 0).
    maf = FakeMafAgent(stream_texts=["ok"], usage_details=None, planning_usage=None, intent=_COUNT_INTENT)
    agent, _, principal = _make_stream_agent(maf, records=[{"result": 1}])

    events = [event async for event in agent.ask("how many flights?", principal=principal)]

    stats = next(event for event in events if event["type"] == "stats")
    # Both calls still count even though their token usage is unknown.
    assert stats["llm_calls"] == 2
    assert stats["tokens"] == {"prompt": None, "completion": None, "total": None}
    assert [call["stage"] for call in stats["calls"]] == ["agent_planning", "answer_generation"]
    assert events[-1] == {"type": "done"}


async def test_ask_as_of_applies_temporal_filter_for_versioned_entity() -> None:
    spec_intent = QueryIntent(entity="Specification", fields=["maxCruiseSpeed_kt"])
    maf = FakeMafAgent(stream_texts=["122 kt."], intent=spec_intent)
    agent, driver, principal = _make_stream_agent(maf, records=[{"maxCruiseSpeed_kt": 122}])

    events = [event async for event in agent.ask("flight cruise speed in 2020?", principal=principal, as_of="2020-01-01")]

    # The built query carried the as-of temporal filter (deterministic, backend-injected).
    assert "$__asOf" in driver.queries[0][0]
    assert driver.queries[0][1]["__asOf"] == "2020-01-01"
    stats = next(event for event in events if event["type"] == "stats")
    assert stats["versioning"]["mode"] == "as-of"
    assert stats["versioning"]["as_of"] == "2020-01-01"
    assert stats["versioning"]["temporal_filter_applied"] is True


async def test_ask_off_topic_question_is_refused() -> None:
    maf = FakeMafAgent(stream_texts=["should not run"], intent=_COUNT_INTENT)
    agent, driver, principal = _make_stream_agent(maf, records=[{"result": 1}])

    events = [event async for event in agent.ask("What is the capital of France?", principal=principal)]

    assert events[0] == {
        "type": "metadata",
        "cypher_used": [],
        "records": [],
        "tools_used": [],
        "intents_used": [],
        "documents_used": [],
    }
    # Off-topic questions short-circuit before the pipeline runs, so no progress events.
    assert not any(event["type"] == "progress" for event in events)
    tokens = [event["text"] for event in events if event["type"] == "token"]
    assert "only answer questions about the aircraft" in "".join(tokens)
    stats = next(event for event in events if event["type"] == "stats")
    assert stats["llm_calls"] == 0
    assert stats["calls"] == []
    # Off-topic requests are audited as refused.
    assert stats["audit"]["outcome"] == "refused_off_topic"
    assert events[-1] == {"type": "done"}
    # Neither the query nor the agent ran.
    assert driver.queries == []
    assert maf.prompts == []


async def test_ask_denies_unauthorized_intent() -> None:
    # public may not see Flight at all; the tool records a denial and relays a refusal,
    # but the agent still produces an answer turn. The denial appears in the audit trail.
    maf = FakeMafAgent(stream_texts=["That information is not available to you."], intent=_COUNT_INTENT)
    agent, driver, principal = _make_stream_agent(maf, records=[{"result": 1}], user="public")

    events = [event async for event in agent.ask("how many flights?", principal=principal)]

    # The unauthorized intent never reached the database.
    assert driver.queries == []
    stats = next(event for event in events if event["type"] == "stats")
    assert stats["record_count"] == 0
    # The denial is recorded in the audit trail.
    assert stats["audit"]["denied"]
    assert events[-1] == {"type": "done"}


async def test_ask_degrades_on_query_error() -> None:
    # The query fails at execution; the agent still runs and answers from the tool's
    # graceful message. The (built) Cypher is surfaced but no rows are returned.
    maf = FakeMafAgent(stream_texts=["I couldn't find that."], intent=_COUNT_INTENT)
    agent, _, principal = _make_stream_agent(maf, query_error=RuntimeError("boom"))

    events = [event async for event in agent.ask("how many flights?", principal=principal)]

    non_progress = [event for event in events if event["type"] != "progress"]
    assert non_progress[0]["type"] == "metadata"
    tokens = [event["text"] for event in events if event["type"] == "token"]
    assert "".join(tokens) == "I couldn't find that."
    stats = next(event for event in events if event["type"] == "stats")
    # The agent still made its planning and answer LLM calls.
    assert stats["llm_calls"] == 2
    assert [call["stage"] for call in stats["calls"]] == ["agent_planning", "answer_generation"]
    assert stats["record_count"] == 0
    assert events[-1] == {"type": "done"}


async def test_ask_emits_error_event_on_generation_failure() -> None:
    maf = FakeMafAgent(stream_texts=["partial"], stream_error=RuntimeError("stream blew up"), intent=_COUNT_INTENT)
    agent, _, principal = _make_stream_agent(maf, records=[{"result": 1}])

    events = [event async for event in agent.ask("how many flights?", principal=principal)]

    # Metadata still precedes the failure; an error event is emitted, then stats + done.
    non_progress = [event for event in events if event["type"] != "progress"]
    assert non_progress[0]["type"] == "metadata"
    assert any(event["type"] == "error" for event in events)
    assert events[-1] == {"type": "done"}


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
    def __init__(self) -> None:
        self.principal: Any = None

    async def ask(self, question: str, principal: Any = None, *, as_of: Any = None) -> Any:
        self.principal = principal
        yield {"type": "metadata", "cypher_used": ["MATCH (n) RETURN count(n)"], "records": [{"count": 1}]}
        yield {"type": "token", "text": "42"}
        yield {
            "type": "stats",
            "model": "fake-model",
            "principal": principal.model_dump(mode="json") if principal is not None else None,
            "llm_calls": 2,
            "tokens": {"prompt": 100, "completion": 10, "total": 110},
            "calls": [],
            "durations_ms": {"retrieval": 1.0, "graph_query": 0.5, "generation": 2.0, "total": 3.0},
            "cypher_count": 1,
            "record_count": 1,
        }
        yield {"type": "done"}


def _use_test_policy() -> None:
    """Point the /ask and /users endpoints at the bundled access policy for tests."""
    app.app.dependency_overrides[app._get_policy] = lambda: PolicyStore.load()


async def test_ask_endpoint_rejects_empty_question() -> None:
    app.app.dependency_overrides[app._get_agent] = lambda: FakeKGAgent()
    _use_test_policy()
    transport = ASGITransport(app=app.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/ask", json={"question": ""})
    finally:
        app.app.dependency_overrides.clear()
    assert response.status_code == 422


async def test_ask_endpoint_streams_ndjson() -> None:
    app.app.dependency_overrides[app._get_agent] = lambda: FakeKGAgent()
    _use_test_policy()
    transport = ASGITransport(app=app.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/ask", json={"question": "How many nodes?", "user": "maintenance_engineer"})
    finally:
        app.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines() if line]
    assert events[0]["type"] == "metadata"
    assert any(event["type"] == "token" and event["text"] == "42" for event in events)
    # The selected user is resolved to a principal and surfaced in the stats event.
    stats = next(event for event in events if event["type"] == "stats")
    assert stats["principal"]["id"] == "maintenance_engineer"
    assert events[-1] == {"type": "done"}


async def test_ask_endpoint_defaults_unknown_user_to_least_privilege() -> None:
    app.app.dependency_overrides[app._get_agent] = lambda: FakeKGAgent()
    _use_test_policy()
    transport = ASGITransport(app=app.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/ask", json={"question": "How many nodes?", "user": "ghost"})
    finally:
        app.app.dependency_overrides.clear()
    events = [json.loads(line) for line in response.text.splitlines() if line]
    stats = next(event for event in events if event["type"] == "stats")
    assert stats["principal"]["id"] == "public"


async def test_users_endpoint_lists_policy_identities() -> None:
    _use_test_policy()
    transport = ASGITransport(app=app.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/users")
    finally:
        app.app.dependency_overrides.clear()
    assert response.status_code == 200
    body = response.json()
    assert body["version"]
    ids = [user["id"] for user in body["users"]]
    assert "public" in ids and "maintenance_engineer" in ids


async def test_ask_endpoint_503_when_agent_unconfigured() -> None:
    app.app.state.agent = None
    transport = ASGITransport(app=app.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ask", json={"question": "anything"})
    assert response.status_code == 503


# ── _append_document_retrieval (surfacing document output to evaluation) ──────


def _bare_agent() -> KnowledgeGraphAgent:
    return object.__new__(KnowledgeGraphAgent)


def test_append_document_retrieval_surfaces_content_for_evaluation() -> None:
    agent = _bare_agent()
    excerpt = DocumentExcerpt(
        documentId="DOC-0001",
        title="Pilot Operating Handbook",
        contentType="text/markdown",
        version=1,
        text="Never-exceed speed (Vne): 163 KIAS",
        truncated=False,
        charCount=34,
    )
    sink: list[dict[str, Any]] = []
    token = retrieval_sink.set(sink)
    try:
        agent._append_document_retrieval("POH", excerpt, 12.0)
    finally:
        retrieval_sink.reset(token)

    assert len(sink) == 1
    entry = sink[0]
    # The selected document's id + full content are surfaced for output scoring,
    document = entry["document"]
    assert document is not None
    assert document["documentId"] == "DOC-0001"
    assert "163 KIAS" in document["content"]
    # but the provenance ``records`` table stays body-free.
    assert entry["records"] == [
        {
            "documentId": "DOC-0001",
            "title": "Pilot Operating Handbook",
            "version": 1,
            "contentType": "text/markdown",
            "charCount": 34,
            "truncated": False,
        }
    ]
    assert "content" not in entry["records"][0]


def test_append_document_retrieval_denied_fetch_has_no_document() -> None:
    agent = _bare_agent()
    sink: list[dict[str, Any]] = []
    token = retrieval_sink.set(sink)
    try:
        agent._append_document_retrieval("Secret", None, 3.0)
    finally:
        retrieval_sink.reset(token)

    assert sink[0]["document"] is None
    assert sink[0]["records"] == []
