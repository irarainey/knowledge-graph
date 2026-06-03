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
import json
import os
from dataclasses import dataclass, field
from typing import Any

from neo4j import Driver, GraphDatabase, RoutingControl
from neo4j_graphrag.exceptions import Text2CypherRetrievalError
from neo4j_graphrag.generation import GraphRAG, RagTemplate
from neo4j_graphrag.llm import AzureOpenAILLM, OpenAILLM
from neo4j_graphrag.llm.base import LLMInterface
from neo4j_graphrag.retrievers import Text2CypherRetriever
from neo4j_graphrag.types import RetrieverResultItem

from neo4j_client import (
    SCHEMA_NODE_PROPERTIES,
    SCHEMA_RELATIONSHIP_PROPERTIES,
    SCHEMA_RELATIONSHIPS,
    Neo4jSettings,
    format_schema,
    to_jsonable,
)

DEFAULT_API_VERSION = "2024-10-21"

# Few-shot question/Cypher pairs that anchor the cypher-generation LLM to this
# graph's exact labels and relationship types.
DEFAULT_EXAMPLES = [
    "USER INPUT: 'How many flights are recorded?' QUERY: MATCH (f:Flight) RETURN count(f) AS flights",
    "USER INPUT: 'What is the total flight time across all flights?' QUERY: MATCH (f:Flight) RETURN sum(f.flightTime_hours) AS totalFlightHours",
    "USER INPUT: 'Which systems does the aircraft have?' QUERY: MATCH (a:Aircraft)-[:HAS_SYSTEM]->(s) RETURN s.name AS system",
    "USER INPUT: 'List the components of the fuel system.' QUERY: MATCH (:FuelSystem)-[:HAS_COMPONENT]->(c) RETURN c.name AS component",
]

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


def fetch_schema_text(driver: Driver, database: str) -> str:
    """Introspect the graph over a synchronous driver and render it as prompt text.

    Uses plain Cypher (no APOC) so it works on a stock Neo4j Community container.
    """

    def rows(query: str) -> list[dict[str, Any]]:
        records, _, _ = driver.execute_query(query, database_=database, routing_=RoutingControl.READ)
        return [dict(record) for record in records]

    schema = {
        "nodes": rows(SCHEMA_NODE_PROPERTIES),
        "relationships": rows(SCHEMA_RELATIONSHIPS),
        "relationshipProperties": rows(SCHEMA_RELATIONSHIP_PROPERTIES),
    }
    return format_schema(schema)


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
            result_formatter=_record_to_item,
            neo4j_database=database,
        )
        prompt_template = RagTemplate(template=RAG_TEMPLATE, expected_inputs=["context", "query_text", "examples"])
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
            return AskResult(answer="I couldn't find an answer to that question in the knowledge graph.")
        except Exception as exc:
            # Degrade gracefully on any LLM/connectivity error rather than 500.
            print(f"/ask failed: {type(exc).__name__}: {exc}")
            return AskResult(answer="I couldn't find an answer to that question in the knowledge graph.")

        retriever_result = response.retriever_result
        metadata = (retriever_result.metadata or {}) if retriever_result else {}
        cypher = metadata.get("cypher")
        items = retriever_result.items if retriever_result else []
        records = [item.metadata["record"] for item in items if item.metadata and "record" in item.metadata]
        return AskResult(
            answer=response.answer,
            cypher_used=[cypher] if cypher else [],
            records=records,
        )

    def close(self) -> None:
        """Close the agent's synchronous Neo4j driver."""
        self._driver.close()
