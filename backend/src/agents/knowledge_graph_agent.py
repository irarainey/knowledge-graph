"""Natural-language querying of the knowledge graph.

The ``/ask`` endpoint answers questions with a deterministic retrieve-then-generate
pipeline:

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
from dataclasses import dataclass, field
from typing import Any

from agent_framework import Agent
from agent_framework.openai import OpenAIChatCompletionClient
from neo4j import Driver, GraphDatabase
from neo4j_graphrag.exceptions import Text2CypherRetrievalError
from neo4j_graphrag.generation import RagTemplate
from neo4j_graphrag.llm.base import LLMInterface
from neo4j_graphrag.retrievers import Text2CypherRetriever
from neo4j_graphrag.types import RetrieverResult

from common.azure_openai import AzureOpenAISettings, build_chat_client, build_llm
from common.graph_schema import fetch_schema_text
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

__all__ = ["AskResult", "AzureOpenAISettings", "KnowledgeGraphAgent"]

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


@dataclass
class AskResult:
    """Outcome of an agent run: the answer plus the Cypher/context it relied on."""

    answer: str
    cypher_used: list[str] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)


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
        # Build the schema from the correct database with APOC-free introspection so
        # the cypher-generation prompt matches the database the queries run against.
        schema = fetch_schema_text(driver, database)
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

    def _install_usage_recorder(self) -> None:
        """Wrap ``self._llm.invoke`` so cypher-generation token usage is captured.

        KNOWN ANTI-PATTERN (deliberate trade-off): this monkey-patches a method on a
        third-party ``neo4j-graphrag`` LLM object and tags it with a private
        ``_kg_usage_wrapped`` flag. It is fragile — it reaches into library internals
        and could break if ``invoke``'s signature changes. We accept it because
        ``Text2CypherRetriever`` calls ``llm.invoke`` to generate Cypher but exposes
        no hook to observe that call's token usage, which the debug telemetry needs.
        The wrapper appends each call's normalized usage to the request-scoped
        :data:`usage_sink` (when one is active). The original bound method is
        preserved and double-wrapping is guarded against. Revisit if neo4j-graphrag
        ever surfaces retriever-level usage.
        """
        if getattr(self._llm, "_kg_usage_wrapped", False):
            return
        original_invoke = self._llm.invoke

        def recording_invoke(*args: Any, **kwargs: Any) -> Any:
            # LLMInterface.invoke(input, message_history=None, system_instruction=None).
            request_input = args[0] if args else kwargs.get("input")
            request_system = args[2] if len(args) >= 3 else kwargs.get("system_instruction")
            start = time.perf_counter()
            response = original_invoke(*args, **kwargs)
            duration_ms = elapsed_ms(start)
            sink = usage_sink.get()
            if sink is not None:
                sink.append(
                    {
                        **normalize_llm_usage(getattr(response, "usage", None)),
                        "duration_ms": duration_ms,
                        "request": build_llm_messages(request_system, request_input),
                    }
                )
            return response

        try:
            self._llm.invoke = recording_invoke  # type: ignore[method-assign]
            self._llm._kg_usage_wrapped = True  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - defensive: some LLMs forbid attr assignment
            print(f"/ask/stream usage recorder not installed: {type(exc).__name__}: {exc}")

    @classmethod
    def from_settings(cls, azure: AzureOpenAISettings, neo4j_settings: Neo4jSettings) -> KnowledgeGraphAgent:
        """Build the agent and its dedicated synchronous Neo4j driver from settings."""
        driver = GraphDatabase.driver(neo4j_settings.uri, auth=(neo4j_settings.username, neo4j_settings.password))
        return cls(build_llm(azure), build_chat_client(azure), driver, database=neo4j_settings.database)

    def _format_prompt(self, question: str, retriever_result: RetrieverResult | None) -> str:
        """Build the answer prompt (rows as context) exactly as the RAG template expects."""
        context = "\n".join(item.content for item in retriever_result.items) if retriever_result else ""
        return self._prompt_template.format(query_text=question, context=context, examples="")

    async def ask(self, question: str) -> AskResult:
        """Answer a natural-language question over the knowledge graph.

        Retrieval (text-to-Cypher) is synchronous, so it runs in a worker thread; the
        retrieved rows are then handed to the MAF agent to generate the answer.
        """
        try:
            retriever_result = await asyncio.to_thread(self._retriever.search, query_text=question)
        except Text2CypherRetrievalError as exc:
            print(f"/ask cypher retrieval failed: {exc}")
            return AskResult(answer=FALLBACK_ANSWER)
        except Exception as exc:
            # Degrade gracefully on any retrieval/connectivity error rather than 500.
            print(f"/ask retrieval failed: {type(exc).__name__}: {exc}")
            return AskResult(answer=FALLBACK_ANSWER)

        cypher_used, records = extract_cypher_and_records(retriever_result)
        prompt = self._format_prompt(question, retriever_result)
        try:
            response = await self._agent.run(prompt)
            answer = response.text
        except Exception as exc:
            print(f"/ask generation failed: {type(exc).__name__}: {exc}")
            answer = FALLBACK_ANSWER
        return AskResult(answer=answer, cypher_used=cypher_used, records=records)

    async def ask_stream(self, question: str) -> AsyncIterator[dict[str, Any]]:
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

        # Bind a fresh sink so the wrapped invoke records this request's cypher-gen
        # usage; reset immediately after retrieval (generation makes no invoke call).
        cypher_usages: list[dict[str, Any]] = []
        sink_token = usage_sink.set(cypher_usages)
        retrieval_start = time.perf_counter()
        retriever_result: RetrieverResult | None = None
        retrieval_error: Exception | None = None
        try:
            retriever_result = await asyncio.to_thread(self._retriever.search, query_text=question)
        except Text2CypherRetrievalError as exc:
            retrieval_error = exc
            print(f"/ask/stream cypher retrieval failed: {exc}")
        except Exception as exc:
            retrieval_error = exc
            print(f"/ask/stream retrieval failed: {type(exc).__name__}: {exc}")
        finally:
            usage_sink.reset(sink_token)
        retrieval_ms = elapsed_ms(retrieval_start)

        if retrieval_error is not None:
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
        yield {"type": "metadata", "cypher_used": cypher_used, "records": records}

        # Hand the retrieved rows to the MAF agent and stream its answer tokens.
        prompt = self._format_prompt(question, retriever_result)
        answer_request = build_llm_messages(self._instructions, prompt)
        answer_usage: dict[str, Any] | None = None
        answer_call_made = False
        generation_start = time.perf_counter()
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
            raise
        except Exception as exc:
            print(f"/ask/stream generation failed: {type(exc).__name__}: {exc}")
            yield {"type": "error", "message": "Answer generation failed."}
        generation_ms = elapsed_ms(generation_start)

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
