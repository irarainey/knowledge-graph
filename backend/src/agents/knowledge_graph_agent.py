"""Natural-language querying of the knowledge graph (structured-intent retrieval + authz).

The ``/ask`` endpoint answers questions with a single **Microsoft Agent Framework** agent
that owns orchestration and is given one **typed** tool:

1. A relevance guardrail (deterministic, no LLM) rejects off-topic questions up front.
2. The agent is **forced** to call the ``query_knowledge_graph`` tool on its first turn.
   The agent does NOT write Cypher: it emits a typed query *intent* (entity, fields,
   filters, optional aggregate). The backend then validates that intent against the acting
   identity's policy and **deterministically builds and runs** a parameterised, read-only
   Cypher query (see :mod:`authz.query_builder`). Authorization is enforced here, outside
   the LLM: unauthorised entities/fields/aggregates are rejected and classified rows are
   filtered out before execution, so unauthorised data never participates in a query.
3. MAF resets the forced tool choice to ``auto`` after one iteration, so the agent then
   generates a concise natural-language answer **from the retrieved rows only**.

A single question therefore makes **two** LLM calls, in order: the agent's tool-planning
turn (which produces the typed intent) and the answer-generation turn. There is **no
cypher-generation LLM call** — turning intent into Cypher is deterministic — which both
removes a leakage channel and is represented faithfully in the debug ``stats`` event.

The agent is built **per request** so its instructions and tool surface are scoped to the
acting identity: only the entities and fields that identity may see are described to the
model, so unauthorised field *names* never reach it.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, LiteralString, cast

from agent_framework import Agent, ChatContext, ChatMiddleware, ChatOptions, FunctionTool, ToolMode, tool
from agent_framework.openai import OpenAIChatCompletionClient
from neo4j import Driver, GraphDatabase, Query, RoutingControl

from authz import (
    Aggregate,
    AuthorizationError,
    Filter,
    PolicyStore,
    Principal,
    QueryIntent,
    build_query,
    redact_records,
)
from common.audit import (
    OUTCOME_ANSWERED,
    OUTCOME_ERROR,
    OUTCOME_REFUSED_OFF_TOPIC,
    build_audit,
    log_audit,
    schema_fingerprint,
)
from common.azure_openai import AzureOpenAISettings, build_chat_client
from common.graph_schema import fetch_schema_text
from common.guardrails import OFF_TOPIC_ANSWER, build_relevance_vocabulary, is_relevant
from common.logging_config import get_logger
from common.query_safety import QuerySafetyError, assert_safe_cypher, row_cap, statement_timeout_seconds
from common.telemetry import (
    elapsed_ms,
    emit_progress,
    emit_safety_denied,
    maf_call_sink,
    normalize_maf_usage,
    progress_sink,
    retrieval_sink,
    safety_sink,
    serialize_maf_messages,
)
from neo4j_client import Neo4jSettings
from prompts import STRUCTURED_AGENT_SYSTEM_PROMPT

__all__ = ["AzureOpenAISettings", "KnowledgeGraphAgent"]

logger = get_logger(__name__)

# Name of the typed retrieval tool exposed to the MAF agent. The agent is forced to call it
# on the first turn (see ``_TOOL_CHOICE``) so every answer is grounded in graph rows.
TOOL_NAME = "query_knowledge_graph"

# Force the agent to call the retrieval tool on the first turn. MAF resets ``required`` to
# auto after one iteration, so the agent retrieves exactly once, then answers from the rows.
_TOOL_CHOICE: ToolMode = {"mode": "required", "required_function_name": TOOL_NAME}

# Pipeline phases surfaced to the streaming client as ``progress`` events so the UI status
# reflects the stage actually in flight (and matches the debug panel's steps): tool-planning
# → deterministic query build → graph query → answer generation.
PROGRESS_PLANNING = "planning"
PROGRESS_CYPHER = "cypher"
PROGRESS_QUERYING = "querying"
PROGRESS_ANSWERING = "answering"


def _install_query_safety(driver: Driver, timeout: float) -> None:
    """Wrap ``driver.execute_query`` to enforce query safety on every statement it runs.

    Two guarantees are added on top of the query builder only ever emitting read-only
    ``MATCH … RETURN``:

    * **Construct safety** — :func:`assert_safe_cypher` rejects procedures, schema
      introspection, ``LOAD CSV``, database switches and multiple statements *before* the
      query is sent, raising :class:`~common.query_safety.QuerySafetyError`. The denial is
      recorded for the request's audit trail.
    * **A per-statement timeout** — every string statement is wrapped in a
      :class:`neo4j.Query` carrying ``timeout`` so no single query can run unbounded.

    The wrap is applied to the agent's own driver instance (not a library object), and is
    idempotent. Schema-introspection runs through it too — those queries are plain read
    Cypher, so they pass cleanly.
    """
    if getattr(driver, "_kg_query_safety_wrapped", False):
        return
    original = driver.execute_query

    def safe_execute_query(*args: Any, **kwargs: Any) -> Any:
        if args:
            query_obj, rest_args, from_args = args[0], args[1:], True
        else:
            query_obj, rest_args, from_args = kwargs.get("query_"), (), False
        text = query_obj.text if isinstance(query_obj, Query) else query_obj
        if isinstance(text, str):
            try:
                assert_safe_cypher(text)
            except QuerySafetyError as exc:
                logger.warning("Query-safety refused a statement: %s", exc)
                emit_safety_denied(str(exc))
                raise
            query_obj = Query(cast(LiteralString, text), timeout=timeout)
        if from_args:
            return original(query_obj, *rest_args, **kwargs)
        kwargs["query_"] = query_obj
        return original(**kwargs)

    driver.execute_query = safe_execute_query  # type: ignore[method-assign]
    driver._kg_query_safety_wrapped = True  # type: ignore[attr-defined]


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
    principal: Principal | None,
    maf_calls: list[dict[str, Any]],
    retrieval_ms: float,
    generation_ms: float,
    total_ms: float,
    cypher_count: int,
    record_count: int,
) -> dict[str, Any]:
    """Assemble the ``stats`` debug event from observed per-call usage and timings.

    Every real LLM call is represented as its own entry in ``calls``, in chronological
    order: the agent's tool-planning turn (which emits the typed query intent) and the
    answer-generation turn. There are exactly **two** LLM calls per answered question —
    turning the intent into Cypher is deterministic (no LLM), so there is no
    cypher-generation call. The MAF turns come from :data:`maf_call_sink` (recorded per
    turn by a chat middleware, since MAF otherwise aggregates usage across turns). Token
    counts aggregate only across calls that actually reported usage; when none did, the
    aggregate stays ``None`` (unknown) rather than ``0``.

    The acting ``principal`` (resolved identity + policy version) is included so the debug
    panel and audit trail can attribute every answer to who asked it and under which
    policy version.
    """
    planning_calls = [call for call in maf_calls if call.get("stage") == "agent_planning"]
    answer_calls = [call for call in maf_calls if call.get("stage") != "agent_planning"]
    # Chronological order: plan the typed query (tool call) → answer from the rows.
    calls: list[dict[str, Any]] = [*planning_calls, *answer_calls]

    def aggregate(key: str) -> int | None:
        values = [call[key] for call in calls if call.get(key) is not None]
        return sum(values) if values else None

    return {
        "type": "stats",
        "model": model,
        "principal": principal.model_dump(mode="json") if principal is not None else None,
        "llm_calls": len(calls),
        "tokens": {"prompt": aggregate("prompt"), "completion": aggregate("completion"), "total": aggregate("total")},
        "calls": calls,
        "durations_ms": {
            "retrieval": retrieval_ms,
            "graph_query": retrieval_ms,
            "generation": generation_ms,
            "total": total_ms,
        },
        "cypher_count": cypher_count,
        "record_count": record_count,
    }


class KnowledgeGraphAgent:
    """Per-request MAF agent that retrieves via a forced, policy-validated typed query tool."""

    def __init__(
        self,
        chat_client: OpenAIChatCompletionClient,
        driver: Driver,
        policy: PolicyStore,
        *,
        database: str,
        model_name: str | None = None,
    ) -> None:
        self._chat_client = chat_client
        self._driver = driver
        self._policy = policy
        self._database = database
        self._model = model_name
        logger.info("Building knowledge-graph agent (database=%s)", database)
        # Build the schema text once: used to fingerprint for audit drift detection and to
        # derive the deterministic, no-LLM relevance vocabulary for the guardrail. The
        # per-request prompt surface is scoped per principal (see ``_build_maf_agent``).
        logger.debug("Fetching graph schema (database=%s)", database)
        schema = fetch_schema_text(driver, database)
        logger.debug("Graph schema fetched (%d characters)", len(schema))
        self._schema_fingerprint = schema_fingerprint(schema)
        self._vocabulary = build_relevance_vocabulary(schema)
        logger.debug("Knowledge-graph agent ready (structured-intent query builder)")

    async def _run_query_tool(self, principal: Principal, intent: QueryIntent) -> str:
        """Validate ``intent`` against policy, run the built query, return rows as JSON.

        Enforcement happens in :func:`authz.build_query`: unauthorised entities/fields/
        aggregates raise :class:`AuthorizationError`, and a clearance filter excludes
        classified rows before execution. Denials are recorded on :data:`safety_sink` (for
        the audit trail) and a short refusal string is returned for the agent to relay,
        rather than raising — so the agent always produces an answer.
        """
        tool_start = time.perf_counter()
        emit_progress(PROGRESS_CYPHER)
        try:
            built = build_query(intent, principal, self._policy)
        except AuthorizationError as exc:
            logger.info("Query intent denied for %s: %s", principal.id, exc)
            emit_safety_denied(str(exc))
            sink = retrieval_sink.get()
            if sink is not None:
                sink.append({"cypher": [], "records": [], "duration_ms": elapsed_ms(tool_start)})
            emit_progress(PROGRESS_ANSWERING)
            return f"Not permitted: {exc} The requested information is not available to this user."

        emit_progress(PROGRESS_QUERYING)
        try:
            records = await asyncio.to_thread(self._execute, built.cypher, built.parameters)
        except Exception:
            logger.exception("/ask graph query failed")
            sink = retrieval_sink.get()
            if sink is not None:
                sink.append({"cypher": [built.cypher], "records": [], "duration_ms": elapsed_ms(tool_start)})
            emit_progress(PROGRESS_ANSWERING)
            return "Retrieval failed; no rows are available for this question."

        records = redact_records(records, built.returned_fields)
        cap = row_cap()
        if len(records) > cap:
            logger.info("Capping retrieval rows from %d to %d (QUERY_ROW_CAP)", len(records), cap)
            records = records[:cap]
        sink = retrieval_sink.get()
        if sink is not None:
            sink.append({"cypher": [built.cypher], "records": records, "duration_ms": elapsed_ms(tool_start)})
        # The graph rows are back; the agent's next turn generates the answer from them.
        emit_progress(PROGRESS_ANSWERING)
        if not records:
            return "No rows were returned from the knowledge graph for that query."
        return json.dumps(records)

    def _execute(self, cypher: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        """Run the deterministically built, parameterised query (read-only) and return rows.

        Synchronous (neo4j driver), so it is invoked via ``asyncio.to_thread``. The driver's
        ``execute_query`` is wrapped by :func:`_install_query_safety`, which re-validates the
        statement and applies the per-statement timeout as defence-in-depth.
        """
        result = self._driver.execute_query(
            cast(LiteralString, cypher),
            parameters_=parameters,
            database_=self._database,
            routing_=RoutingControl.READ,
        )
        return [dict(record) for record in result.records]

    def _build_tool(self, principal: Principal) -> FunctionTool:
        """Build the typed retrieval tool, bound (via closure) to the acting principal.

        The agent fills in a structured intent; the backend validates it against
        ``principal``'s policy and builds the Cypher. Binding the principal here (rather than
        passing it as a tool argument) keeps it out of the model's reach.
        """

        @tool(name=TOOL_NAME, description="Query the aircraft knowledge graph by describing what to fetch; returns matching rows.")
        async def query_knowledge_graph(
            entity: Annotated[str, "The entity (node label) to query, e.g. 'Flight'. Must be one listed in the catalog."],
            fields: Annotated[list[str], "Fields to return; leave empty to return all available fields for the entity."] = [],  # noqa: B006
            filters: Annotated[list[Filter], "Optional field comparisons to narrow the rows."] = [],  # noqa: B006
            aggregate: Annotated[Aggregate | None, "Optional aggregate (count/avg/sum/min/max) instead of returning rows."] = None,
            limit: Annotated[int | None, "Optional maximum number of rows to return."] = None,
        ) -> str:
            intent = QueryIntent(entity=entity, fields=fields, filters=filters, aggregate=aggregate, limit=limit)
            return await self._run_query_tool(principal, intent)

        return query_knowledge_graph

    def _build_maf_agent(self, principal: Principal) -> Agent:
        """Construct a per-request MAF agent with instructions and tools scoped to ``principal``.

        Only the entities and fields the principal may see are described in the instructions
        (via :meth:`PolicyStore.describe_surface`), so unauthorised field *names* never reach
        the model. The agent is forced to call the typed retrieval tool on its first turn,
        then answers from the rows. The turn-recorder middleware captures each LLM call
        (planning + answer) individually for the debug ``stats`` event.
        """
        instructions = STRUCTURED_AGENT_SYSTEM_PROMPT.format(surface=self._policy.describe_surface(principal))
        return Agent(
            client=self._chat_client,
            instructions=instructions,
            name="knowledge-graph",
            tools=[self._build_tool(principal)],
            default_options=ChatOptions(tool_choice=_TOOL_CHOICE),
            middleware=[_MafTurnRecorder(instructions)],
        )

    @classmethod
    def from_settings(cls, azure: AzureOpenAISettings, neo4j_settings: Neo4jSettings, policy: PolicyStore) -> KnowledgeGraphAgent:
        """Build the agent and its dedicated synchronous Neo4j driver from settings."""
        logger.debug("Creating synchronous Neo4j driver for agent at %s (database=%s)", neo4j_settings.uri, neo4j_settings.database)
        driver = GraphDatabase.driver(neo4j_settings.uri, auth=(neo4j_settings.username, neo4j_settings.password))
        agent = cls(build_chat_client(azure), driver, policy, database=neo4j_settings.database, model_name=azure.deployment)
        # Enforce query safety (construct denylist + per-statement timeout) on every query the
        # tool runs. Installed *after* the constructor's schema introspection so that one-off
        # init-time `CALL` (e.g. version/schema checks) is not refused — only the structured
        # queries run at request time are guarded (each also re-validated by the builder).
        _install_query_safety(driver, statement_timeout_seconds())
        return agent

    async def ask(self, question: str, principal: Principal | None = None) -> AsyncIterator[dict[str, Any]]:
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
        * ``{"type": "stats", ...}`` — debug telemetry (model, acting principal, llm_calls,
          tokens, durations, counts), emitted once just before ``done``.
        * ``{"type": "done"}`` — always emitted last.

        ``principal`` is the resolved acting identity (see :mod:`authz`); it is recorded in
        the ``stats`` event so every answer can be attributed to who asked it and under
        which policy version, and it scopes the per-request agent (instructions + tool
        surface) so only data the identity may see is described to the model. A
        deterministic relevance guardrail rejects off-topic questions up front. The MAF
        agent then orchestrates: it is forced to call ``query_knowledge_graph`` (emitting a
        typed intent the backend validates against policy and turns into Cypher
        deterministically) on its first turn, then streams an answer from the rows. The tool
        stashes its Cypher/rows/timing on :data:`retrieval_sink` so this method can emit
        ``metadata`` and ``stats`` without re-running retrieval.
        """
        # Default-deny: an unresolved identity becomes the least-privilege default principal.
        if principal is None:
            principal = self._policy.resolve_principal(None)
        model = self._model
        total_start = time.perf_counter()
        actor = principal.id
        logger.info("Answering question (acting as %s): %s", actor, question)

        # Guardrail: reject questions unrelated to the knowledge graph before any LLM
        # call or database query (deterministic, no LLM).
        if not is_relevant(question, self._vocabulary):
            logger.info("Question rejected by relevance guardrail (off-topic): %s", question)
            yield {"type": "metadata", "cypher_used": [], "records": []}
            yield {"type": "token", "text": OFF_TOPIC_ANSWER}
            stats = _build_stats(
                model=model,
                principal=principal,
                maf_calls=[],
                retrieval_ms=0.0,
                generation_ms=0.0,
                total_ms=elapsed_ms(total_start),
                cypher_count=0,
                record_count=0,
            )
            stats["audit"] = log_audit(
                build_audit(
                    principal=principal,
                    question=question,
                    outcome=OUTCOME_REFUSED_OFF_TOPIC,
                    schema_fingerprint=self._schema_fingerprint,
                    cypher=[],
                    record_count=0,
                    llm_calls=0,
                    denied=[],
                    duration_ms=elapsed_ms(total_start),
                )
            )
            yield stats
            yield {"type": "done"}
            return

        # Bind per-request sinks: the retrieval tool appends its Cypher/rows/timing, the
        # turn-recorder middleware appends each MAF LLM call (planning + answer), and
        # authorization/query-safety denials are collected for the audit trail.
        retrievals: list[dict[str, Any]] = []
        maf_calls: list[dict[str, Any]] = []
        denials: list[str] = []
        retrieval_token = retrieval_sink.set(retrievals)
        maf_call_token = maf_call_sink.set(maf_calls)
        safety_token = safety_sink.set(denials)

        # Build the agent for THIS request so its instructions and tool surface are scoped to
        # the acting principal (only data the identity may see is described to the model).
        request_agent = self._build_maf_agent(principal)

        # Merge the agent's answer-token stream with backend-emitted ``progress`` phase
        # events onto a single queue. The query-build and graph-query steps run inside the
        # forced tool with no answer tokens, so without this the client's status would stall
        # on one label. Each pipeline stage calls ``emit_progress`` at its boundary; that
        # lands here and is forwarded to the UI.
        loop = asyncio.get_running_loop()
        merged: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        def emit_progress_event(phase: str) -> None:
            # The graph query runs via ``asyncio.to_thread``, so progress can be emitted from
            # either the loop thread (tool orchestration) or a worker thread. Put directly
            # when already on the loop for deterministic ordering; marshal from worker threads.
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
                stream = request_agent.run(question, stream=True)
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
        run_failed = False
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
                    run_failed = True
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
            retrieval_sink.reset(retrieval_token)
            maf_call_sink.reset(maf_call_token)
            safety_sink.reset(safety_token)

        # Defensive: emit metadata even if the agent produced no tool call or text.
        if not metadata_emitted:
            cyphers, records = aggregate_retrieval()
            yield {"type": "metadata", "cypher_used": cyphers, "records": records}

        cypher_used, records = aggregate_retrieval()
        retrieval_ms = round(sum(entry["duration_ms"] for entry in retrievals), 1)
        generation_ms = round(max(elapsed_ms(agent_start) - retrieval_ms, 0.0), 1)
        logger.info("Answer generated in %.1fms (total %.1fms)", generation_ms, elapsed_ms(total_start))

        stats = _build_stats(
            model=model,
            principal=principal,
            maf_calls=maf_calls,
            retrieval_ms=retrieval_ms,
            generation_ms=generation_ms,
            total_ms=elapsed_ms(total_start),
            cypher_count=len(cypher_used),
            record_count=len(records),
        )
        stats["audit"] = log_audit(
            build_audit(
                principal=principal,
                question=question,
                outcome=OUTCOME_ERROR if run_failed else OUTCOME_ANSWERED,
                schema_fingerprint=self._schema_fingerprint,
                cypher=cypher_used,
                record_count=len(records),
                llm_calls=stats["llm_calls"],
                denied=denials,
                duration_ms=elapsed_ms(total_start),
            )
        )
        yield stats
        yield {"type": "done"}

    def close(self) -> None:
        """Close the agent's synchronous Neo4j driver."""
        self._driver.close()
