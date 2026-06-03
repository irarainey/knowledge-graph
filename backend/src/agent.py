"""Natural-language querying of the knowledge graph via Neo4j GraphRAG.

The ``/ask`` endpoint answers questions using a **text-to-Cypher** GraphRAG
pattern built on the ``neo4j-graphrag`` package:

1. :class:`~neo4j_graphrag.retrievers.Text2CypherRetriever` asks an LLM to write a
   Cypher query from the user's question and the live graph schema, validates it
   is read-only (via ``EXPLAIN``), runs it, and returns the matching rows.
2. :class:`~neo4j_graphrag.generation.GraphRAG` feeds those rows back to the LLM to
   generate a concise natural-language answer.

The package enforces read-only execution itself, so the model can never mutate the
graph even if it generates a write/delete.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from neo4j import Driver, GraphDatabase, RoutingControl
from neo4j_graphrag.exceptions import Text2CypherRetrievalError
from neo4j_graphrag.generation import GraphRAG, RagTemplate
from neo4j_graphrag.llm import AzureOpenAILLM, OpenAILLM
from neo4j_graphrag.llm.base import LLMInterface
from neo4j_graphrag.retrievers import Text2CypherRetriever
from neo4j_graphrag.types import RetrieverResult, RetrieverResultItem

from neo4j_client import (
    SCHEMA_NODE_SAMPLES,
    SCHEMA_RELATIONSHIP_PROPERTIES,
    SCHEMA_RELATIONSHIPS,
    Neo4jSettings,
    to_jsonable,
)

DEFAULT_API_VERSION = "2024-10-21"

# Shown to the user when retrieval or generation fails, so the API degrades
# gracefully instead of returning a 500.
FALLBACK_ANSWER = "I couldn't find an answer to that question in the knowledge graph."

# Few-shot question/Cypher pairs that anchor the cypher-generation LLM to this
# graph's exact labels, relationship types and conventions: ISO-string dates cast
# with date(), inlined literals (no $parameters), full hierarchy traversal for
# component/part questions, and aggregation for totals.
DEFAULT_EXAMPLES = [
    "USER INPUT: 'How many flights are recorded?' QUERY: MATCH (f:Flight) RETURN count(f) AS flights",
    "USER INPUT: 'Which aerodromes has the aircraft flown to?' "
    "QUERY: MATCH (:Flight)-[:ARRIVES_AT]->(a:Aerodrome) RETURN DISTINCT a.name AS aerodrome",
    "USER INPUT: 'How many flying hours has the engine had since 2026-05-25?' "
    "QUERY: MATCH (ac:Aircraft)-[:HAS_SYSTEM]->(:System)-[:HAS_COMPONENT]->(e:PistonEngine) "
    "MATCH (f:Flight)-[:USES_AIRCRAFT]->(ac) WHERE date(f.date) >= date('2026-05-25') "
    "RETURN e.name AS engine, count(f) AS flights, sum(coalesce(f.flightTime_hours, 0)) AS hours",
    "USER INPUT: 'How much ground distance has the front tyre covered between 2026-05-01 and 2026-05-31?' "
    "QUERY: MATCH (f:Flight)-[:USES_AIRCRAFT]->(:Aircraft)-[:HAS_SYSTEM]->(:LandingGearSystem)"
    "-[:HAS_COMPONENT]->(:NoseWheel)-[:HAS_PART]->(tyre:Tyre) "
    "WHERE date(f.date) >= date('2026-05-01') AND date(f.date) <= date('2026-05-31') "
    "MATCH (f)-[:HAS_PHASE]->(phase:FlightPhase) WHERE phase.groundRoll_m IS NOT NULL "
    "RETURN tyre.name AS tyre, sum(phase.groundRoll_m) AS totalGroundDistance_m",
]

# Cypher-generation prompt. Replaces the package default so we can inject
# domain rules. Text2CypherRetriever substitutes {schema}, {examples} and
# {query_text}; any other literal braces would need doubling.
CYPHER_GENERATION_PROMPT = """\
Task: write a single read-only Cypher query that answers the user's question using the \
Neo4j graph described below.

Graph schema:
{schema}

Rules:
- Use ONLY the node labels, relationship types and properties that appear in the schema. \
Never invent or guess names. For example, an engine has no "total hours" property — derive \
engine flying hours from the flightTime_hours of the Flights that use the aircraft.
- Inline literal values directly in the query. NEVER use query parameters such as $since or \
$start — the query is executed exactly as written with no parameters supplied.
- Dates are stored as ISO-8601 STRINGS (e.g. "2026-05-20"). For ANY date comparison you MUST \
cast both sides with date(), and use explicit AND for ranges, e.g. \
`WHERE date(f.date) >= date('2026-05-01') AND date(f.date) <= date('2026-05-31')`. \
Never compare Flight.date directly against a date value.
- Systems, components and parts form a hierarchy: \
(:Aircraft)-[:HAS_SYSTEM]->(:System)-[:HAS_COMPONENT]->(component)-[:HAS_PART]->(part). \
To reach a specific component or part, traverse the full path; do not read a property off a \
shortcut node. Only traverse this hierarchy when the question is about a system, component, \
part, engine, tyre or maintenance item. For questions purely about flights, hours or dates, \
query :Flight directly.
- Flights connect to the aircraft via (:Flight)-[:USES_AIRCRAFT]->(:Aircraft) and to their \
phases via (:Flight)-[:HAS_PHASE]->(:FlightPhase). Specific phases also carry labels such as \
:Taxi, :Takeoff and :Landing.
- Wrap nullable numeric properties in coalesce(...) when summing, and add `IS NOT NULL` filters \
for properties that may be absent.
- If the question asks for a count, sum, total, average, maximum or minimum, return that \
aggregate with a clear alias; otherwise return the relevant rows with clear column aliases.

Examples:
{examples}

Question:
{query_text}

Return only the Cypher query, with no backticks, comments or any other text.
"""

# A label shared by at least this many distinct sibling labels is treated as a
# generic super-label (e.g. System, Component); its property union is noisy, so we
# render only property names for it rather than typed examples.
GENERIC_LABEL_SIBLING_THRESHOLD = 4

# Answer-generation prompt. Keeps the numeric-fidelity and concise/factual
# guidance from the previous agent. Only {context}, {examples} and {query_text}
# are substituted — any literal braces would need doubling.
RAG_TEMPLATE = """\
You are a knowledge-graph assistant. You answer questions about a small piston-engine \
light aircraft — its systems, components, flights, aerodromes and maintenance — that is \
modelled as a Neo4j graph.

Use only the rows in the context below; they were retrieved from the graph for this \
question. Base your answer solely on those rows. Never invent or assume values. If the \
context is empty or does not contain the answer, say so plainly.

Report every numeric value EXACTLY as it appears in the context. Do NOT round, truncate \
or reformat numbers — if a value is 2.23, write 2.23, not 2.2 or 2. When you state a \
number, make clear what it counts or measures (e.g. "2.23 flying hours across 4 flights").

Keep answers concise and factual.

Context:
{context}

Examples:
{examples}

Question:
{query_text}

Answer:
"""


@dataclass
class AskResult:
    """Outcome of an agent run: the answer plus the Cypher/context it relied on."""

    answer: str
    cypher_used: list[str] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AzureOpenAISettings:
    endpoint: str
    api_key: str
    deployment: str
    api_version: str

    @classmethod
    def from_env(cls) -> AzureOpenAISettings:
        missing = [v for v in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT") if not os.environ.get(v)]
        if missing:
            raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")
        return cls(
            endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION),
        )


def build_llm(settings: AzureOpenAISettings) -> LLMInterface:
    """Create a neo4j-graphrag LLM for the configured Azure OpenAI endpoint.

    Azure AI Foundry / "v1" deployments expose an OpenAI-compatible surface at
    ``<resource>/openai/v1`` and are reached with the plain OpenAI client via
    ``base_url``. Classic Azure OpenAI resources use deployment routing via
    ``azure_endpoint`` + ``api_version``.
    """
    endpoint = settings.endpoint.rstrip("/")
    if "/openai/v1" in endpoint:
        return OpenAILLM(model_name=settings.deployment, base_url=endpoint, api_key=settings.api_key)
    return AzureOpenAILLM(
        model_name=settings.deployment,
        azure_endpoint=endpoint,
        api_version=settings.api_version,
        api_key=settings.api_key,
    )


def _record_to_item(record: Any) -> RetrieverResultItem:
    """Format a Neo4j record into a retriever item, keeping the row JSON-serialisable.

    The JSON ``content`` becomes the LLM context; the structured dict is stashed in
    ``metadata`` so the API can return the raw rows it answered from.
    """
    data = to_jsonable(dict(record))
    return RetrieverResultItem(content=json.dumps(data), metadata={"record": data})


def _extract_cypher_and_records(retriever_result: RetrieverResult | None) -> tuple[list[str], list[dict[str, Any]]]:
    """Pull the generated Cypher and the JSON-serialisable rows out of a retriever result."""
    metadata = (retriever_result.metadata or {}) if retriever_result else {}
    cypher = metadata.get("cypher")
    items = retriever_result.items if retriever_result else []
    records = [item.metadata["record"] for item in items if item.metadata and "record" in item.metadata]
    return ([cypher] if cypher else [], records)


def _scalar_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    return type(value).__name__


def _example_literal(value: Any) -> str:
    try:
        text = json.dumps(to_jsonable(value), default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > 40:
        text = text[:39] + "…"
    return text


def _build_node_section(node_rows: list[dict[str, Any]]) -> list[str]:
    """Render per-label properties with types/examples, trimming generic super-labels.

    For each label we keep the first non-null example value seen for each property.
    Labels shared across many sibling labels (super-labels like ``System``) get a
    names-only listing so their large property unions don't swamp the prompt.
    """
    siblings: dict[str, set[str]] = {}
    examples: dict[str, dict[str, Any]] = {}
    for row in node_rows:
        labels: list[str] = row.get("labels") or []
        props: dict[str, Any] = row.get("props") or {}
        label_set = set(labels)
        for label in labels:
            siblings.setdefault(label, set()).update(label_set - {label})
            store = examples.setdefault(label, {})
            for key, value in props.items():
                if value is not None and key not in store:
                    store[key] = value

    lines = ["Node labels and their properties:"]
    for label in sorted(examples):
        props = examples[label]
        if not props:
            lines.append(f"- {label}: (no properties)")
        elif len(siblings.get(label, set())) >= GENERIC_LABEL_SIBLING_THRESHOLD:
            lines.append(f"- {label}: {', '.join(sorted(props))}")
        else:
            rendered = ", ".join(f"{key} ({_scalar_type(props[key])}, e.g. {_example_literal(props[key])})" for key in sorted(props))
            lines.append(f"- {label}: {rendered}")
    return lines


def fetch_schema_text(driver: Driver, database: str) -> str:
    """Introspect the graph over a synchronous driver and render it as prompt text.

    Uses plain Cypher (no APOC) so it works on a stock Neo4j Community container.
    Node properties are shown with inferred types and example values so the LLM can
    see, for instance, that ``Flight.date`` is an ISO string that needs casting.
    """

    def rows(query: str) -> list[dict[str, Any]]:
        records, _, _ = driver.execute_query(query, database_=database, routing_=RoutingControl.READ)
        return [dict(record) for record in records]

    lines = _build_node_section(rows(SCHEMA_NODE_SAMPLES))

    lines.append("")
    lines.append("Relationships (startLabels)-[TYPE]->(endLabels):")
    for row in rows(SCHEMA_RELATIONSHIPS):
        start = ":".join(row.get("startLabels", []))
        end = ":".join(row.get("endLabels", []))
        lines.append(f"- ({start})-[{row['type']}]->({end})")

    rel_props = rows(SCHEMA_RELATIONSHIP_PROPERTIES)
    if rel_props:
        lines.append("")
        lines.append("Relationship properties:")
        for row in rel_props:
            props = ", ".join(row.get("properties", []))
            lines.append(f"- {row['type']}: {props}")

    return "\n".join(lines)


class KnowledgeGraphAgent:
    """Text-to-Cypher GraphRAG over the Neo4j knowledge graph (neo4j-graphrag)."""

    def __init__(
        self,
        llm: LLMInterface,
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
            result_formatter=_record_to_item,
            neo4j_database=database,
        )
        prompt_template = RagTemplate(template=RAG_TEMPLATE, expected_inputs=["context", "query_text", "examples"])
        # Keep direct references so the streaming path can drive retrieval and answer
        # generation itself (neo4j-graphrag's GraphRAG/LLM offer no token streaming).
        self._llm = llm
        self._retriever = retriever
        self._prompt_template = prompt_template
        self._rag = GraphRAG(retriever=retriever, llm=llm, prompt_template=prompt_template)

    @classmethod
    def from_settings(cls, azure: AzureOpenAISettings, neo4j_settings: Neo4jSettings) -> KnowledgeGraphAgent:
        """Build the agent and its dedicated synchronous Neo4j driver from settings."""
        driver = GraphDatabase.driver(neo4j_settings.uri, auth=(neo4j_settings.username, neo4j_settings.password))
        return cls(build_llm(azure), driver, database=neo4j_settings.database)

    async def ask(self, question: str) -> AskResult:
        """Answer a natural-language question over the knowledge graph.

        GraphRAG is synchronous, so it runs in a worker thread to avoid blocking the
        event loop. The shared Neo4j driver is thread-safe for concurrent queries.
        """
        return await asyncio.to_thread(self._ask_sync, question)

    def _ask_sync(self, question: str) -> AskResult:
        try:
            response = self._rag.search(query_text=question, return_context=True)
        except Text2CypherRetrievalError as exc:
            print(f"/ask cypher retrieval failed: {exc}")
            return AskResult(answer=FALLBACK_ANSWER)
        except Exception as exc:
            # Degrade gracefully on any LLM/connectivity error rather than 500.
            print(f"/ask failed: {type(exc).__name__}: {exc}")
            return AskResult(answer=FALLBACK_ANSWER)

        retriever_result = response.retriever_result
        cypher_used, records = _extract_cypher_and_records(retriever_result)
        return AskResult(answer=response.answer, cypher_used=cypher_used, records=records)

    async def ask_stream(self, question: str) -> AsyncIterator[dict[str, Any]]:
        """Answer a question while streaming the LLM's tokens.

        Yields newline-delimited-JSON-friendly event dicts in order:

        * ``{"type": "metadata", "cypher_used": [...], "records": [...]}`` — emitted
          once, after retrieval, before any answer tokens.
        * ``{"type": "token", "text": "..."}`` — repeated, the streamed answer.
        * ``{"type": "error", "message": "..."}`` — only on failure.
        * ``{"type": "done"}`` — always emitted last.

        Retrieval (text-to-Cypher) is synchronous, so it runs in a worker thread; the
        answer is then streamed natively from the async OpenAI client.
        """
        try:
            retriever_result = await asyncio.to_thread(self._retriever.search, query_text=question)
        except Text2CypherRetrievalError as exc:
            print(f"/ask/stream cypher retrieval failed: {exc}")
            yield {"type": "metadata", "cypher_used": [], "records": []}
            yield {"type": "token", "text": FALLBACK_ANSWER}
            yield {"type": "done"}
            return
        except Exception as exc:
            print(f"/ask/stream retrieval failed: {type(exc).__name__}: {exc}")
            yield {"type": "metadata", "cypher_used": [], "records": []}
            yield {"type": "token", "text": FALLBACK_ANSWER}
            yield {"type": "done"}
            return

        cypher_used, records = _extract_cypher_and_records(retriever_result)
        yield {"type": "metadata", "cypher_used": cypher_used, "records": records}

        # Build the answer prompt exactly as GraphRAG.search would, so streamed and
        # non-streamed answers stay consistent.
        context = "\n".join(item.content for item in retriever_result.items)
        prompt = self._prompt_template.format(query_text=question, context=context, examples="")
        system_instruction = self._prompt_template.system_instructions

        try:
            async for text in self._stream_answer(prompt, system_instruction):
                yield {"type": "token", "text": text}
        except asyncio.CancelledError:
            # Client disconnected — let cancellation propagate so the upstream
            # OpenAI stream is torn down promptly.
            raise
        except Exception as exc:
            print(f"/ask/stream generation failed: {type(exc).__name__}: {exc}")
            yield {"type": "error", "message": "Answer generation failed."}

        yield {"type": "done"}

    async def _stream_answer(self, prompt: str, system_instruction: str | None) -> AsyncIterator[str]:
        """Stream answer tokens from the LLM's async OpenAI client.

        Falls back to a single non-streaming ``invoke`` if the underlying client is
        not the expected OpenAI client (keeps the agent usable with custom LLMs).
        """
        client = getattr(self._llm, "async_client", None)
        model = getattr(self._llm, "model_name", None)
        if client is None or model is None:
            result = await asyncio.to_thread(self._llm.invoke, prompt, None, system_instruction)
            yield result.content
            return

        # Reuse the package's message construction so streaming matches invoke().
        get_messages = getattr(self._llm, "get_messages", None)
        if callable(get_messages):
            messages = get_messages(prompt, None, system_instruction)
        else:
            messages = []
            if system_instruction:
                messages.append({"role": "system", "content": system_instruction})
            messages.append({"role": "user", "content": prompt})

        model_params = getattr(self._llm, "model_params", None) or {}
        stream = await client.chat.completions.create(model=model, messages=messages, stream=True, **model_params)
        try:
            async for chunk in stream:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                text = getattr(delta, "content", None) if delta is not None else None
                if text:
                    yield text
        finally:
            close = getattr(stream, "aclose", None) or getattr(stream, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

    def close(self) -> None:
        """Close the agent's synchronous Neo4j driver."""
        self._driver.close()
