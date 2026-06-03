"""FastAPI service exposing the Neo4j knowledge graph over HTTP.

Endpoints:

* ``POST /query`` runs an arbitrary Cypher query (with optional parameters) and
  returns the resulting records.
* ``POST /ask`` answers a natural-language question by letting an LLM agent write
  read-only Cypher against the graph (text-to-Cypher GraphRAG).

Connection and model details come from the environment / ``backend/.env``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from neo4j.exceptions import Neo4jError
from pydantic import BaseModel, Field

from agent import AzureOpenAISettings, KnowledgeGraphAgent
from neo4j_client import Neo4jClient, Neo4jSettings, load_env


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The Cypher query to execute.")
    parameters: dict[str, Any] = Field(default_factory=dict, description="Named query parameters ($name) referenced by the query.")
    database: str | None = Field(default=None, description="Override the target database (defaults to NEO4J_DATABASE).")


class QueryResponse(BaseModel):
    columns: list[str] = Field(description="Return column names, in order.")
    records: list[dict[str, Any]] = Field(description="Result rows keyed by column name.")
    count: int = Field(description="Number of rows returned.")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="A natural-language question about the knowledge graph.")


class AskResponse(BaseModel):
    answer: str = Field(description="The agent's natural-language answer.")
    cypher_used: list[str] = Field(description="The Cypher queries the agent ran to find the answer.")
    records: list[dict[str, Any]] = Field(description="The graph rows the agent retrieved as context.")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the shared Neo4j driver (and GraphRAG agent, if configured) on startup."""
    load_env()
    neo4j_settings = Neo4jSettings.from_env()
    client = Neo4jClient(neo4j_settings)
    await client.verify_connectivity()
    app.state.neo4j = client

    # The agent is optional: /query works without Azure OpenAI credentials. It owns
    # a dedicated synchronous Neo4j driver (neo4j-graphrag is sync-only).
    try:
        app.state.agent = KnowledgeGraphAgent.from_settings(AzureOpenAISettings.from_env(), neo4j_settings)
    except RuntimeError as exc:
        app.state.agent = None
        print(f"Azure OpenAI not configured ({exc}); /ask endpoint disabled.")

    try:
        yield
    finally:
        if app.state.agent is not None:
            app.state.agent.close()
        await client.close()


app = FastAPI(
    title="Knowledge Graph API",
    description="Query the Neo4j knowledge graph with Cypher, or ask natural-language questions.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def get_client(request: Request) -> Neo4jClient:
    """Return the shared Neo4j client created during app startup."""
    return request.app.state.neo4j  # type: ignore[no-any-return]


def get_agent(request: Request) -> KnowledgeGraphAgent:
    """Return the shared knowledge-graph agent, or 503 if Azure OpenAI is unconfigured."""
    agent = request.app.state.agent
    if agent is None:
        raise HTTPException(status_code=503, detail="The /ask endpoint requires Azure OpenAI settings (AZURE_OPENAI_*) in the environment.")
    return agent  # type: ignore[no-any-return]


@app.post("/query", response_model=QueryResponse)
async def run_query(payload: QueryRequest, client: Annotated[Neo4jClient, Depends(get_client)]) -> QueryResponse:
    try:
        columns, records = await client.run_query(payload.query, payload.parameters, payload.database)
    except Neo4jError as exc:
        # Surface Cypher/syntax/constraint errors to the caller as a 400.
        raise HTTPException(status_code=400, detail=exc.message or str(exc)) from exc
    return QueryResponse(columns=columns, records=records, count=len(records))


@app.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest, agent: Annotated[KnowledgeGraphAgent, Depends(get_agent)]) -> AskResponse:
    result = await agent.ask(payload.question)
    return AskResponse(answer=result.answer, cypher_used=result.cypher_used, records=result.records)
