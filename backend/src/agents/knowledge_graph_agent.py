"""Natural-language querying of the knowledge graph.

The ``/ask`` endpoint answers questions with a deterministic
retrieve-then-generate pipeline:

1. :class:`~neo4j_graphrag.retrievers.Text2CypherRetriever` asks an LLM to write a
   Cypher query from the user's question and the live graph schema, validates it
   is read-only (via ``EXPLAIN``), runs it, and returns the matching rows.
2. A **Microsoft Agent Framework** :class:`~agent_framework.Agent` (backed by an
   :class:`~agent_framework.openai.OpenAIChatCompletionClient`) is given those rows
   as context and generates a concise natural-language answer.

Retrieval always runs first, so the agent only ever answers from rows actually
retrieved from the graph. ``neo4j-graphrag`` enforces read-only execution itself,
so the model can never mutate the graph even if it generates a write/delete.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator
from typing import Any

from agent_framework import Agent
from agent_framework.openai import OpenAIChatCompletionClient
from neo4j import Driver, GraphDatabase
from neo4j_graphrag.exceptions import Text2CypherRetrievalError
from neo4j_graphrag.generation import RagTemplate
from neo4j_graphrag.llm.base import LLMInterface
from neo4j_graphrag.retrievers import Text2CypherRetriever
from neo4j_graphrag.types import RetrieverResult
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from common.azure_openai import AzureOpenAISettings, build_chat_client, build_llm
from common.graph_schema import fetch_schema_text
from common.logging_config import get_logger
from common.retrieval import extract_cypher_and_records, record_to_item
from common.telemetry import (
    build_llm_messages,
    elapsed_ms,
    empty_usage,
    normalize_llm_usage,
    normalize_maf_usage,
    usage_sink,
)
from neo4j_client import Neo4jSettings
from prompts import CYPHER_GENERATION_PROMPT, DEFAULT_EXAMPLES, RAG_TEMPLATE

__all__ = ["AzureOpenAISettings", "KnowledgeGraphAgent"]

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

# Shown to the user when retrieval or generation fails, so the API degrades
# gracefully instead of returning a 500.
FALLBACK_ANSWER = "I couldn't find an answer to that question in the knowledge graph."


def _build_stats(
    *,
    model: str | None,
    cypher_usages: list[dict[str, Any]],
    answer_usage: dict[str, Any] | None,
    answer_request: list[dict[str, str]] | None,
    answer_call_made: bool,
    retrieval_ms: float,
    generation_ms: float,
    total_ms: float,
    cypher_count: int,
    record_count: int,
) -> dict[str, Any]:
    """Assemble the ``stats`` debug event from observed per-call usage and timings.

    Token counts are aggregated only across calls that actually reported usage;
    when none did, the aggregate stays ``None`` (unknown) rather than ``0``. Each
    call carries its own ``duration_ms`` and the ``request`` messages sent to the
    LLM; the graph-query duration is whatever's left of retrieval after the
    cypher-generation LLM call.
    """
    calls: list[dict[str, Any]] = [{"stage": "cypher_generation", **usage} for usage in cypher_usages]
    cypher_llm_ms = sum(usage.get("duration_ms") or 0.0 for usage in cypher_usages)
    if answer_call_made:
        answer_call = {
            "stage": "answer_generation",
            **(answer_usage or empty_usage()),
            "duration_ms": round(generation_ms, 1),
            "request": answer_request or [],
        }
        calls.append(answer_call)

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
    """Deterministic text-to-Cypher retrieval (neo4j-graphrag) + MAF answer generation."""

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
        prompt_template = RagTemplate(template=RAG_TEMPLATE, expected_inputs=["context", "query_text", "examples"])
        # neo4j-graphrag drives Cypher generation (self._llm) and retrieval; the
        # Microsoft Agent Framework agent generates the natural-language answer from
        # the retrieved rows and supports native token streaming.
        self._llm = llm
        self._retriever = retriever
        self._prompt_template = prompt_template
        self._instructions = prompt_template.system_instructions
        self._agent = Agent(client=chat_client, instructions=self._instructions, name="knowledge-graph")
        self._install_usage_recorder()
        logger.debug("Knowledge-graph agent ready (retriever + MAF answer agent constructed)")

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

    def _format_prompt(self, question: str, retriever_result: RetrieverResult | None) -> str:
        """Build the answer prompt (rows as context) exactly as the RAG template expects."""
        context = "\n".join(item.content for item in retriever_result.items) if retriever_result else ""
        return self._prompt_template.format(query_text=question, context=context, examples="")

    async def _retrieve(self, question: str) -> tuple[RetrieverResult | None, Exception | None]:
        """Run text-to-Cypher retrieval in a worker thread, degrading gracefully on error.

        Retrieval (neo4j-graphrag) is synchronous, so it runs via ``asyncio.to_thread``.
        Returns ``(result, None)`` on success or ``(None, error)`` on any failure, so
        the caller can fall back to ``FALLBACK_ANSWER`` rather than surfacing a 500.
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
        """Answer a question while streaming the LLM's tokens.

        Yields newline-delimited-JSON-friendly event dicts in order:

        * ``{"type": "metadata", "cypher_used": [...], "records": [...]}`` — emitted
          once, after retrieval, before any answer tokens.
        * ``{"type": "token", "text": "..."}`` — repeated, the streamed answer.
        * ``{"type": "error", "message": "..."}`` — only on failure.
        * ``{"type": "stats", ...}`` — debug telemetry (model, llm_calls, tokens,
          durations, counts), emitted once just before ``done``.
        * ``{"type": "done"}`` — always emitted last.

        Retrieval (text-to-Cypher) is synchronous, so it runs in a worker thread; the
        answer is then streamed natively from the async OpenAI client.
        """
        model = getattr(self._llm, "model_name", None)
        total_start = time.perf_counter()
        logger.info("Answering question: %s", question)

        # Bind a fresh sink so the wrapped invoke records this request's cypher-gen
        # usage; reset immediately after retrieval (generation makes no invoke call).
        cypher_usages: list[dict[str, Any]] = []
        sink_token = usage_sink.set(cypher_usages)
        retrieval_start = time.perf_counter()
        try:
            retriever_result, retrieval_error = await self._retrieve(question)
        finally:
            usage_sink.reset(sink_token)
        retrieval_ms = elapsed_ms(retrieval_start)

        if retrieval_error is not None:
            logger.warning("Retrieval failed after %.1fms; returning fallback answer", retrieval_ms)
            yield {"type": "metadata", "cypher_used": [], "records": []}
            yield {"type": "token", "text": FALLBACK_ANSWER}
            yield _build_stats(
                model=model,
                cypher_usages=cypher_usages,
                answer_usage=None,
                answer_request=None,
                answer_call_made=False,
                retrieval_ms=retrieval_ms,
                generation_ms=0.0,
                total_ms=elapsed_ms(total_start),
                cypher_count=0,
                record_count=0,
            )
            yield {"type": "done"}
            return

        cypher_used, records = extract_cypher_and_records(retriever_result)
        logger.info("Retrieval succeeded in %.1fms: %d cypher query(ies), %d record(s)", retrieval_ms, len(cypher_used), len(records))
        logger.debug("Generated cypher: %s", cypher_used)
        yield {"type": "metadata", "cypher_used": cypher_used, "records": records}

        # Hand the retrieved rows to the MAF agent and stream its answer tokens.
        prompt = self._format_prompt(question, retriever_result)
        logger.debug("Built answer prompt (%d characters) from %d retrieved record(s)", len(prompt), len(records))
        answer_request = build_llm_messages(self._instructions, prompt)
        answer_usage: dict[str, Any] | None = None
        answer_call_made = False
        generation_start = time.perf_counter()
        logger.debug("Starting answer generation (streaming tokens from the MAF agent)")
        try:
            stream = self._agent.run(prompt, stream=True)
            answer_call_made = True
            async for update in stream:
                text = getattr(update, "text", None)
                if text:
                    yield {"type": "token", "text": text}
            final: Any = stream.get_final_response()
            if inspect.isawaitable(final):
                final = await final
            answer_usage = normalize_maf_usage(getattr(final, "usage_details", None))
        except asyncio.CancelledError:
            # Client disconnected — let cancellation propagate so the upstream
            # model stream is torn down promptly.
            logger.info("Answer generation cancelled (client disconnected)")
            raise
        except Exception as exc:
            logger.exception("/ask generation failed: %s", type(exc).__name__)
            yield {"type": "error", "message": "Answer generation failed."}
        generation_ms = elapsed_ms(generation_start)
        logger.info("Answer generated in %.1fms (total %.1fms)", generation_ms, elapsed_ms(total_start))

        yield _build_stats(
            model=model,
            cypher_usages=cypher_usages,
            answer_usage=answer_usage,
            answer_request=answer_request,
            answer_call_made=answer_call_made,
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
