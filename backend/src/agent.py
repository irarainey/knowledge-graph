"""Natural-language querying of the knowledge graph via Microsoft Agent Framework.

The agent answers questions by writing read-only Cypher against the live Neo4j
schema (a "text-to-Cypher" GraphRAG pattern). The graph is exposed to the model as
two tools:

* ``get_graph_schema`` — node labels and their properties, the relationship types
  that connect them, and any relationship properties.
* ``query_knowledge_graph`` — run a read-only Cypher query and return the rows.

Queries execute inside a Neo4j read transaction, so the model can never mutate the
graph even if it generates a write/delete.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Annotated, Any

from agent_framework import tool
from agent_framework_openai import OpenAIChatClient

from neo4j_client import Neo4jClient

DEFAULT_API_VERSION = "2024-10-21"


def azure_client_kwargs(settings: AzureOpenAISettings) -> dict[str, Any]:
    """Choose the right OpenAIChatClient wiring for the configured Azure endpoint.

    Azure AI Foundry / "v1" deployments expose an OpenAI-compatible surface at
    ``<resource>/openai/v1`` and must be reached via ``base_url``. Classic Azure
    OpenAI resources use deployment routing via ``azure_endpoint`` + ``api_version``.
    """
    endpoint = settings.endpoint.rstrip("/")
    kwargs: dict[str, Any] = {"model": settings.deployment, "api_key": settings.api_key}
    if "/openai/v1" in endpoint:
        kwargs["base_url"] = endpoint
    else:
        kwargs["azure_endpoint"] = endpoint
        kwargs["api_version"] = settings.api_version
    return kwargs


# Clauses that write to the graph. Used only for a friendly early error — the read
# transaction is the real safeguard.
_WRITE_CLAUSE = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV|CALL\s*\{[^}]*\b(CREATE|MERGE|DELETE|SET|REMOVE)\b)",
    re.IGNORECASE,
)

INSTRUCTIONS = """\
You are a knowledge-graph assistant. You answer questions about a small aircraft \
(a piston-engine light aircraft, its systems, components, flights, aerodromes and \
maintenance) that is modelled as a Neo4j graph.

To answer a question:
1. ALWAYS call `get_graph_schema` first, before writing any Cypher. Study the exact \
node labels, property names and relationship types it returns.
2. Validate every part of your query against the schema before running it. Only use \
node labels, relationship types and property names that appear verbatim in the schema. \
Confirm that each relationship you traverse actually connects the labels you use, in \
the direction the schema shows. Never guess, invent, abbreviate or pluralise names.
3. Write a single, syntactically valid read-only Cypher query (MATCH/OPTIONAL \
MATCH/WHERE/RETURN/WITH/ORDER BY only) and call `query_knowledge_graph`. Use query \
parameters ($name) for literal values. Property names and string values are \
case-sensitive — match the schema and data exactly.
4. If a query returns no rows or an error, do not give up immediately. Re-read the \
schema, check your labels/relationships/properties and directions, and try a corrected \
query (e.g. relax over-constrained patterns or fix a mistyped property) before \
concluding there is no answer.
5. Base your answer only on the rows actually returned. If, after valid queries, the \
data genuinely contains no answer, say so plainly. Never invent or assume values.

Reporting numbers:
- Report every numeric value EXACTLY as returned by the query. Do NOT round, truncate \
or reformat numbers. If a value is 2.23, write 2.23, not 2.2 or 2. Do not apply any \
`round()`, `toInteger()` or similar rounding in your Cypher either — return the raw \
values.
- When you state a number, make clear what it counts or sums (e.g. "2.23 flying hours \
across 4 flights").

Keep answers concise and factual.\
"""


def ensure_read_only(cypher: str) -> None:
    """Raise ``ValueError`` if the query obviously attempts to write to the graph."""
    if _WRITE_CLAUSE.search(cypher):
        raise ValueError("Only read-only queries are allowed; this query appears to modify the graph.")


def format_schema(schema: dict[str, list[dict[str, Any]]]) -> str:
    """Render a schema dict (from ``Neo4jClient.fetch_schema``) as compact prompt text."""
    lines: list[str] = ["Node labels and their properties:"]
    for row in schema.get("nodes", []):
        props = ", ".join(row.get("properties", []))
        lines.append(f"- {row['label']}: {props}")

    lines.append("")
    lines.append("Relationships (startLabels)-[TYPE]->(endLabels):")
    for row in schema.get("relationships", []):
        start = ":".join(row.get("startLabels", []))
        end = ":".join(row.get("endLabels", []))
        lines.append(f"- ({start})-[{row['type']}]->({end})")

    rel_props = schema.get("relationshipProperties", [])
    if rel_props:
        lines.append("")
        lines.append("Relationship properties:")
        for row in rel_props:
            props = ", ".join(row.get("properties", []))
            lines.append(f"- {row['type']}: {props}")
    return "\n".join(lines)


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


def build_chat_client(settings: AzureOpenAISettings) -> OpenAIChatClient:
    """Create an Agent Framework chat client targeting Azure OpenAI."""
    return OpenAIChatClient(**azure_client_kwargs(settings))


class KnowledgeGraphAgent:
    """Wraps a chat client and the Neo4j graph as a text-to-Cypher GraphRAG agent."""

    def __init__(self, chat_client: OpenAIChatClient, neo4j_client: Neo4jClient, *, instructions: str = INSTRUCTIONS) -> None:
        self._client = chat_client
        self._neo4j = neo4j_client
        self._instructions = instructions

    def _build_tools(self, result: AskResult) -> list[Any]:
        neo4j = self._neo4j

        @tool
        async def get_graph_schema() -> str:
            """Return the knowledge graph schema: node labels with their properties, the relationship types, and how labels connect."""
            schema = await neo4j.fetch_schema()
            return format_schema(schema)

        @tool
        async def query_knowledge_graph(
            cypher: Annotated[str, "A single read-only Cypher query (MATCH/RETURN). Writes are rejected."],
            parameters: Annotated[dict[str, Any] | None, "Named parameters referenced as $name in the query."] = None,
        ) -> str:
            """Run a read-only Cypher query against the knowledge graph and return the matching rows as JSON."""
            try:
                ensure_read_only(cypher)
                columns, records = await neo4j.run_read_query(cypher, parameters or {})
            except Exception as exc:
                # Report any failure back to the model so it can revise and retry.
                return f"Query failed: {exc}. Revise the Cypher and try again."
            result.cypher_used.append(cypher)
            result.records.extend(records)
            return json.dumps({"columns": columns, "records": records})

        return [get_graph_schema, query_knowledge_graph]

    async def ask(self, question: str) -> AskResult:
        """Answer a natural-language question over the knowledge graph."""
        result = AskResult(answer="")
        agent = self._client.as_agent(
            name="knowledge-graph",
            instructions=self._instructions,
            tools=self._build_tools(result),
        )
        response = await agent.run(question)
        result.answer = response.text
        return result
