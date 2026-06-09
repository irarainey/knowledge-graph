"""Natural-language querying of the knowledge graph.

The ``/ask`` endpoint answers questions with a single **Microsoft Agent Framework**
agent that owns orchestration and is given one tool:

1. A relevance guardrail (deterministic, no LLM) rejects off-topic questions up front.
2. The agent is **forced** to call the ``search_knowledge_graph`` tool on its first
   turn. That tool runs neo4j-graphrag's
   :class:`~neo4j_graphrag.retrievers.Text2CypherRetriever` — an LLM writes a read-only
   Cypher query from the question and live schema, it is validated read-only (via
   ``EXPLAIN``), run, and the rows returned.
3. MAF resets the forced tool choice to ``auto`` after one iteration, so the agent then
   generates a concise natural-language answer **from the retrieved rows only**.

A single question therefore makes **three** LLM calls, in order: the agent's
tool-planning turn, the cypher-generation call (inside the tool), and the
answer-generation turn. MAF aggregates token usage across its own turns, so a
:class:`_MafTurnRecorder` chat middleware records each turn individually; combined with
the cypher-generation call captured separately, the debug ``stats`` event represents all
three LLM calls faithfully.

Forcing the tool keeps the grounding guarantee of the previous deterministic pipeline
(the agent only ever answers from rows actually retrieved) while moving orchestration
into native MAF. ``neo4j-graphrag`` enforces read-only execution itself, so the model
can never mutate the graph even if it generates a write/delete.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

from agent_framework import Agent, ChatContext, ChatMiddleware, ChatOptions, FunctionTool, ToolMode, tool
from agent_framework.openai import OpenAIChatCompletionClient
from neo4j import Driver, GraphDatabase
from neo4j_graphrag.exceptions import Text2CypherRetrievalError
from neo4j_graphrag.llm.base import LLMInterface
from neo4j_graphrag.retrievers import Text2CypherRetriever
from neo4j_graphrag.types import RetrieverResult
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from common.azure_openai import AzureOpenAISettings, build_chat_client, build_llm
from common.graph_schema import fetch_schema_text
from common.guardrails import OFF_TOPIC_ANSWER, build_relevance_vocabulary, is_relevant
from common.logging_config import get_logger
from common.retrieval import extract_cypher_and_records, record_to_item
from common.telemetry import (
    build_llm_messages,
    elapsed_ms,
    emit_progress,
    maf_call_sink,
    normalize_llm_usage,
    normalize_maf_usage,
    progress_sink,
    retrieval_sink,
    serialize_maf_messages,
    usage_sink,
)
from neo4j_client import Neo4jSettings
from prompts import AGENT_SYSTEM_PROMPT, CYPHER_GENERATION_PROMPT, DEFAULT_EXAMPLES

__all__ = ["AzureOpenAISettings", "KnowledgeGraphAgent"]

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

# Name of the retrieval tool exposed to the MAF agent. The agent is forced to call it
# on the first turn (see ``_TOOL_CHOICE``) so every answer is grounded in graph rows.
TOOL_NAME = "search_knowledge_graph"

# Force the agent to call the retrieval tool on the first turn. neo4j-graphrag-style
# determinism in an agentic shell: MAF resets ``required`` to auto after one iteration,
# so the agent retrieves exactly once, then generates its answer from the rows.
_TOOL_CHOICE: ToolMode = {"mode": "required", "required_function_name": TOOL_NAME}

# Pipeline phases surfaced to the streaming client as ``progress`` events so the UI
# status reflects the stage actually in flight. Ordered to match the request workflow
# (and the debug panel's four steps): tool-planning → cypher generation → graph query
# → answer generation.
PROGRESS_PLANNING = "planning"
PROGRESS_CYPHER = "cypher"
PROGRESS_QUERYING = "querying"
PROGRESS_ANSWERING = "answering"


def _response_has_function_call(response: Any) -> bool:
    """True if a finalized ``ChatResponse`` contains a tool/function call.

    Used to label an agent turn: a turn that emits a function call is the
    tool-planning turn; otherwise it is the answer-generation turn.
    """
    for message in getattr(response, "messages", None) or []:
        for content in getattr(message, "contents", None) or []:
            if getattr(content, "type", None) == "function_call":
                return True
    return False


class _MafTurnRecorder(ChatMiddleware):
    """Records each MAF agent turn (one real LLM call) into :data:`maf_call_sink`.

    The Microsoft Agent Framework aggregates ``usage_details`` across every turn of a
    run, which would hide the distinct tool-planning and answer-generation LLM calls.
    This chat middleware fires once per turn, capturing that turn's request messages,
    normalized token usage and duration so the debug ``stats`` event can represent every
    LLM call individually. Streaming responses only expose usage once finalized, so a
    ``stream_result_hook`` reads it when the turn's stream completes.
    """

    def __init__(self, instructions: str) -> None:
        self._instructions = instructions

    async def process(self, context: ChatContext, call_next: Any) -> None:
        start = time.perf_counter()
        # The system instructions are sent on every turn (via options, not as a message),
        # so prepend them to faithfully represent the request the model received.
        request = [{"role": "system", "content": self._instructions}, *serialize_maf_messages(context.messages)]
        await call_next()

        def record(response: Any) -> Any:
            sink = maf_call_sink.get()
            if sink is not None:
                stage = "agent_planning" if _response_has_function_call(response) else "answer_generation"
                sink.append(
                    {
                        "stage": stage,
                        **normalize_maf_usage(getattr(response, "usage_details", None)),
                        "duration_ms": elapsed_ms(start),
                        "request": request,
                    }
                )
            return response

        if context.stream:
            context.stream_result_hooks.append(record)
        elif context.result is not None:
            record(context.result)


def _build_stats(
    *,
    model: str | None,
    cypher_usages: list[dict[str, Any]],
    maf_calls: list[dict[str, Any]],
    retrieval_ms: float,
    generation_ms: float,
    total_ms: float,
    cypher_count: int,
    record_count: int,
) -> dict[str, Any]:
    """Assemble the ``stats`` debug event from observed per-call usage and timings.

    Every real LLM call is represented as its own entry in ``calls``, in chronological
    order: the agent's tool-planning turn, the cypher-generation call (inside the
    retrieval tool), then the answer-generation turn. The MAF turns come from
    :data:`maf_call_sink` (recorded per turn by a chat middleware, since MAF otherwise
    aggregates usage across turns); the cypher call comes from :data:`usage_sink`. Token
    counts aggregate only across calls that actually reported usage; when none did, the
    aggregate stays ``None`` (unknown) rather than ``0``. The graph-query duration is
    whatever's left of retrieval after the cypher-generation LLM call.
    """
    planning_calls = [call for call in maf_calls if call.get("stage") == "agent_planning"]
    answer_calls = [call for call in maf_calls if call.get("stage") != "agent_planning"]
    cypher_calls = [{"stage": "cypher_generation", **usage} for usage in cypher_usages]
    # Chronological order: plan the tool call → generate Cypher (in the tool) → answer.
    calls: list[dict[str, Any]] = [*planning_calls, *cypher_calls, *answer_calls]
    cypher_llm_ms = sum(usage.get("duration_ms") or 0.0 for usage in cypher_usages)

    def aggregate(key: str) -> int | None:
        values = [call[key] for call in calls if call.get(key) is not None]
        return sum(values) if values else None

    return {
        "type": "stats",
        "model": model,
        "llm_calls": len(calls),
        "tokens": {"prompt": aggregate("prompt"), "completion": aggregate("completion"), "total": aggregate("total")},
        "calls": calls,
        "durations_ms": {
            "retrieval": retrieval_ms,
            "graph_query": round(max(retrieval_ms - cypher_llm_ms, 0.0), 1),
            "generation": generation_ms,
            "total": total_ms,
        },
        "cypher_count": cypher_count,
        "record_count": record_count,
    }


class KnowledgeGraphAgent:
    """MAF agent that retrieves via a forced neo4j-graphrag text-to-Cypher tool, then answers."""

    def __init__(
        self,
        llm: LLMInterface,
        chat_client: OpenAIChatCompletionClient,
        driver: Driver,
        *,
        database: str,
        examples: list[str] | None = None,
    ) -> None:
        self._driver = driver
        logger.info("Building knowledge-graph agent (database=%s)", database)
        # Build the schema from the correct database with APOC-free introspection so
        # the cypher-generation prompt matches the database the queries run against.
        logger.debug("Fetching graph schema for cypher-generation prompt (database=%s)", database)
        schema = fetch_schema_text(driver, database)
        logger.debug("Graph schema fetched (%d characters)", len(schema))
        retriever = Text2CypherRetriever(
            driver=driver,
            llm=llm,
            neo4j_schema=schema,
            examples=examples if examples is not None else DEFAULT_EXAMPLES,
            custom_prompt=CYPHER_GENERATION_PROMPT,
            result_formatter=record_to_item,
            neo4j_database=database,
        )
        self._llm = llm
        self._retriever = retriever
        self._instructions = AGENT_SYSTEM_PROMPT
        # Deterministic, no-LLM relevance gate built from the live schema vocabulary.
        self._vocabulary = build_relevance_vocabulary(schema)
        # The MAF agent owns orchestration: it is forced to call the retrieval tool on
        # its first turn, then generates the answer from the rows it returns. The turn
        # recorder middleware captures each LLM call (planning + answer) individually.
        self._agent = Agent(
            client=chat_client,
            instructions=self._instructions,
            name="knowledge-graph",
            tools=[self._build_tool()],
            default_options=ChatOptions(tool_choice=_TOOL_CHOICE),
            middleware=[_MafTurnRecorder(self._instructions)],
        )
        self._install_usage_recorder()
        logger.debug("Knowledge-graph agent ready (MAF agent + forced retrieval tool constructed)")

    async def _run_retrieval_tool(self, question: str) -> str:
        """Run retrieval, stash Cypher/rows/timing on :data:`retrieval_sink`, return rows as JSON.

        Degrades gracefully: on retrieval failure or no rows it returns a short message
        the agent can relay rather than raising, so the agent always produces an answer.
        """
        tool_start = time.perf_counter()
        emit_progress(PROGRESS_CYPHER)
        retriever_result, retrieval_error = await self._retrieve(question)
        cypher, records = extract_cypher_and_records(retriever_result)
        sink = retrieval_sink.get()
        if sink is not None:
            sink.append({"cypher": cypher, "records": records, "duration_ms": elapsed_ms(tool_start)})
        # The graph rows are back; the agent's next turn generates the answer from them.
        emit_progress(PROGRESS_ANSWERING)
        if retrieval_error is not None:
            return "Retrieval failed; no rows are available for this question."
        if not records:
            return "No rows were returned from the knowledge graph for that query."
        return json.dumps(records)

    def _build_tool(self) -> FunctionTool:
        """Wrap text-to-Cypher retrieval as the MAF tool the agent is forced to call."""

        @tool(name=TOOL_NAME, description="Query the aircraft knowledge graph and return matching rows.")
        async def search_knowledge_graph(
            question: Annotated[str, "A natural-language question about the aircraft knowledge graph."],
        ) -> str:
            return await self._run_retrieval_tool(question)

        return search_knowledge_graph

    def _install_usage_recorder(self) -> None:
        """Wrap ``self._llm.invoke`` to capture cypher-generation usage and tracing.

        KNOWN ANTI-PATTERN (deliberate trade-off): this monkey-patches a method on a
        third-party ``neo4j-graphrag`` LLM object and tags it with a private
        ``_kg_usage_wrapped`` flag. It is fragile — it reaches into library internals
        and could break if ``invoke``'s signature changes. We accept it because
        ``Text2CypherRetriever`` calls ``llm.invoke`` to generate Cypher but exposes
        no hook to observe that call, which both the debug telemetry and observability
        need. The wrapper appends each call's normalized usage to the request-scoped
        :data:`usage_sink` (when one is active) and emits a ``gen_ai`` OpenTelemetry
        span. The original bound method is preserved and double-wrapping is guarded
        against. Revisit if neo4j-graphrag ever surfaces retriever-level hooks.

        Why the span is needed: cypher generation runs through neo4j-graphrag's own
        OpenAI client, which the Microsoft Agent Framework's instrumentation does not
        see (only answer generation, via the MAF chat client, is auto-traced). Without
        this span, only one of the two LLM calls per question would appear in telemetry.
        """
        if getattr(self._llm, "_kg_usage_wrapped", False):
            return
        original_invoke = self._llm.invoke
        model = getattr(self._llm, "model_name", None)

        def recording_invoke(*args: Any, **kwargs: Any) -> Any:
            # LLMInterface.invoke(input, message_history=None, system_instruction=None).
            request_input = args[0] if args else kwargs.get("input")
            request_system = args[2] if len(args) >= 3 else kwargs.get("system_instruction")
            # Emit a gen_ai span so the cypher-generation LLM call shows up in telemetry
            # alongside the MAF-traced answer-generation call.
            with tracer.start_as_current_span(f"chat {model}" if model else "chat") as span:
                span.set_attribute("gen_ai.operation.name", "chat")
                span.set_attribute("gen_ai.system", "openai")
                if model:
                    span.set_attribute("gen_ai.request.model", model)
                start = time.perf_counter()
                try:
                    response = original_invoke(*args, **kwargs)
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(StatusCode.ERROR, type(exc).__name__)
                    raise
                duration_ms = elapsed_ms(start)
                usage = normalize_llm_usage(getattr(response, "usage", None))
                if usage["prompt"] is not None:
                    span.set_attribute("gen_ai.usage.input_tokens", usage["prompt"])
                if usage["completion"] is not None:
                    span.set_attribute("gen_ai.usage.output_tokens", usage["completion"])
            logger.debug("Cypher-generation LLM call completed in %.1fms", duration_ms)
            # Cypher is generated; the retriever now runs it against Neo4j.
            emit_progress(PROGRESS_QUERYING)
            sink = usage_sink.get()
            if sink is not None:
                sink.append(
                    {
                        **usage,
                        "duration_ms": duration_ms,
                        "request": build_llm_messages(request_system, request_input),
                    }
                )
            return response

        try:
            self._llm.invoke = recording_invoke  # type: ignore[method-assign]
            self._llm._kg_usage_wrapped = True  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - defensive: some LLMs forbid attr assignment
            logger.warning("/ask usage recorder not installed: %s: %s", type(exc).__name__, exc)

    @classmethod
    def from_settings(cls, azure: AzureOpenAISettings, neo4j_settings: Neo4jSettings) -> KnowledgeGraphAgent:
        """Build the agent and its dedicated synchronous Neo4j driver from settings."""
        logger.debug("Creating synchronous Neo4j driver for agent at %s (database=%s)", neo4j_settings.uri, neo4j_settings.database)
        driver = GraphDatabase.driver(neo4j_settings.uri, auth=(neo4j_settings.username, neo4j_settings.password))
        return cls(build_llm(azure), build_chat_client(azure), driver, database=neo4j_settings.database)

    async def _retrieve(self, question: str) -> tuple[RetrieverResult | None, Exception | None]:
        """Run text-to-Cypher retrieval in a worker thread, degrading gracefully on error.

        Retrieval (neo4j-graphrag) is synchronous, so it runs via ``asyncio.to_thread``.
        Returns ``(result, None)`` on success or ``(None, error)`` on any failure, so the
        retrieval tool can return a graceful message rather than surfacing a 500.
        """
        try:
            logger.debug("Starting text-to-cypher retrieval (running generated Cypher against the graph)")
            result = await asyncio.to_thread(self._retriever.search, query_text=question)
            logger.debug("Text-to-cypher retrieval returned %d item(s)", len(result.items) if result else 0)
            return result, None
        except Text2CypherRetrievalError as exc:
            logger.warning("/ask cypher retrieval failed: %s", exc)
            return None, exc
        except Exception as exc:
            # Degrade gracefully on any retrieval/connectivity error rather than 500.
            logger.exception("/ask retrieval failed: %s", type(exc).__name__)
            return None, exc

    async def ask(self, question: str) -> AsyncIterator[dict[str, Any]]:
        """Answer a question while streaming the agent's tokens.

        Yields newline-delimited-JSON-friendly event dicts in order:

        * ``{"type": "progress", "phase": "..."}`` — repeated, as the pipeline advances
          through its stages (``planning`` → ``cypher`` → ``querying`` → ``answering``)
          so the client can show which stage is in flight. Off-topic questions skip
          these (no pipeline runs).
        * ``{"type": "metadata", "cypher_used": [...], "records": [...]}`` — emitted
          once, after the agent's forced retrieval tool runs, before any answer tokens.
        * ``{"type": "token", "text": "..."}`` — repeated, the streamed answer.
        * ``{"type": "error", "message": "..."}`` — only on failure.
        * ``{"type": "stats", ...}`` — debug telemetry (model, llm_calls, tokens,
          durations, counts), emitted once just before ``done``.
        * ``{"type": "done"}`` — always emitted last.

        A deterministic relevance guardrail rejects off-topic questions up front. The
        MAF agent then orchestrates: it is forced to call ``search_knowledge_graph``
        (text-to-Cypher retrieval) on its first turn, then streams an answer from the
        rows. The tool stashes its Cypher/rows/timing on :data:`retrieval_sink` so this
        method can emit ``metadata`` and ``stats`` without re-running retrieval.
        """
        model = getattr(self._llm, "model_name", None)
        total_start = time.perf_counter()
        logger.info("Answering question: %s", question)

        # Guardrail: reject questions unrelated to the knowledge graph before any LLM
        # call or database query (deterministic, no LLM).
        if not is_relevant(question, self._vocabulary):
            logger.info("Question rejected by relevance guardrail (off-topic): %s", question)
            yield {"type": "metadata", "cypher_used": [], "records": []}
            yield {"type": "token", "text": OFF_TOPIC_ANSWER}
            yield _build_stats(
                model=model,
                cypher_usages=[],
                maf_calls=[],
                retrieval_ms=0.0,
                generation_ms=0.0,
                total_ms=elapsed_ms(total_start),
                cypher_count=0,
                record_count=0,
            )
            yield {"type": "done"}
            return

        # Bind per-request sinks: the wrapped ``invoke`` records cypher-generation usage,
        # the retrieval tool appends its Cypher/rows/timing, and the turn-recorder
        # middleware appends each MAF LLM call (planning + answer).
        cypher_usages: list[dict[str, Any]] = []
        retrievals: list[dict[str, Any]] = []
        maf_calls: list[dict[str, Any]] = []
        usage_token = usage_sink.set(cypher_usages)
        retrieval_token = retrieval_sink.set(retrievals)
        maf_call_token = maf_call_sink.set(maf_calls)

        # Merge the agent's answer-token stream with backend-emitted ``progress`` phase
        # events onto a single queue. The cypher-generation and graph-query steps run
        # inside the forced tool with no answer tokens, so without this the client's
        # status would stall on one label for seconds. Each pipeline stage calls
        # ``emit_progress`` at its boundary; that lands here and is forwarded to the UI.
        loop = asyncio.get_running_loop()
        merged: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        def emit_progress_event(phase: str) -> None:
            # Cypher generation runs via ``asyncio.to_thread``, so progress can be emitted
            # from either the loop thread (tool orchestration) or a worker thread (the
            # cypher recorder). Put directly when already on the loop for deterministic
            # ordering; marshal onto the loop from worker threads.
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                merged.put_nowait(("progress", phase))
            else:
                loop.call_soon_threadsafe(merged.put_nowait, ("progress", phase))

        progress_token = progress_sink.set(emit_progress_event)

        def aggregate_retrieval() -> tuple[list[str], list[dict[str, Any]]]:
            cyphers = [cypher for entry in retrievals for cypher in entry["cypher"]]
            records = [row for entry in retrievals for row in entry["records"]]
            return cyphers, records

        async def pump() -> None:
            """Drive the agent run, forwarding each stream update onto the merged queue."""
            try:
                stream = self._agent.run(question, stream=True)
                async for update in stream:
                    await merged.put(("update", update))
                # Drain the final response so the turn-recorder's stream hooks fire and
                # record each MAF call's usage (only available once the stream finalizes).
                final: Any = stream.get_final_response()
                if inspect.isawaitable(final):
                    await final
                await merged.put(("end", None))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await merged.put(("error", exc))

        metadata_emitted = False
        agent_start = time.perf_counter()
        logger.debug("Starting agent run (forced retrieval tool, then streamed answer)")
        pump_task = asyncio.ensure_future(pump())
        # First phase up front: the agent is selecting its retrieval tool (planning turn).
        yield {"type": "progress", "phase": PROGRESS_PLANNING}
        try:
            while True:
                kind, payload = await merged.get()
                if kind == "progress":
                    yield {"type": "progress", "phase": payload}
                    continue
                if kind == "error":
                    logger.error("/ask agent run failed", exc_info=payload)
                    if not metadata_emitted:
                        cyphers, records = aggregate_retrieval()
                        yield {"type": "metadata", "cypher_used": cyphers, "records": records}
                        metadata_emitted = True
                    yield {"type": "error", "message": "Answer generation failed."}
                    break
                if kind == "end":
                    break
                # kind == "update": an answer-stream chunk from the agent.
                update = payload
                # The agent calls retrieval first; once rows are stashed, emit the
                # metadata event before streaming any answer tokens.
                if not metadata_emitted and retrievals:
                    cyphers, records = aggregate_retrieval()
                    logger.info("Retrieval tool returned %d cypher query(ies), %d record(s)", len(cyphers), len(records))
                    logger.debug("Generated cypher: %s", cyphers)
                    yield {"type": "metadata", "cypher_used": cyphers, "records": records}
                    metadata_emitted = True
                text = getattr(update, "text", None)
                if text:
                    yield {"type": "token", "text": text}
        except asyncio.CancelledError:
            # Client disconnected — cancel the agent run so the upstream model stream is
            # torn down promptly, then let cancellation propagate.
            logger.info("Answer generation cancelled (client disconnected)")
            raise
        finally:
            if not pump_task.done():
                pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task
            progress_sink.reset(progress_token)
            usage_sink.reset(usage_token)
            retrieval_sink.reset(retrieval_token)
            maf_call_sink.reset(maf_call_token)

        # Defensive: emit metadata even if the agent produced no tool call or text.
        if not metadata_emitted:
            cyphers, records = aggregate_retrieval()
            yield {"type": "metadata", "cypher_used": cyphers, "records": records}

        cypher_used, records = aggregate_retrieval()
        retrieval_ms = round(sum(entry["duration_ms"] for entry in retrievals), 1)
        generation_ms = round(max(elapsed_ms(agent_start) - retrieval_ms, 0.0), 1)
        logger.info("Answer generated in %.1fms (total %.1fms)", generation_ms, elapsed_ms(total_start))

        yield _build_stats(
            model=model,
            cypher_usages=cypher_usages,
            maf_calls=maf_calls,
            retrieval_ms=retrieval_ms,
            generation_ms=generation_ms,
            total_ms=elapsed_ms(total_start),
            cypher_count=len(cypher_used),
            record_count=len(records),
        )
        yield {"type": "done"}

    def close(self) -> None:
        """Close the agent's synchronous Neo4j driver."""
        self._driver.close()
