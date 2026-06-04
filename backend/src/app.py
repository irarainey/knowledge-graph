"""FastAPI service exposing the Neo4j knowledge graph over HTTP.

Endpoints:

* ``POST /query`` runs an arbitrary Cypher query (with optional parameters) and
  returns the resulting records.
* ``POST /ask`` answers a natural-language question by letting an LLM agent
  write read-only Cypher against the graph (text-to-Cypher GraphRAG), streaming the
  answer back as newline-delimited JSON events.

Connection and model details come from the environment / ``backend/.env``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from neo4j.exceptions import Neo4jError

from agents import AzureOpenAISettings, KnowledgeGraphAgent
from common.env import load_env
from models import AskRequest, QueryRequest, QueryResponse
from neo4j_client import Neo4jClient, Neo4jSettings


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the shared Neo4j driver (and knowledge-graph agent, if configured) on startup."""
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
    lifespan=_lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _get_client(request: Request) -> Neo4jClient:
    """Return the shared Neo4j client created during app startup."""
    return request.app.state.neo4j  # type: ignore[no-any-return]


def _get_agent(request: Request) -> KnowledgeGraphAgent:
    """Return the shared knowledge-graph agent, or 503 if Azure OpenAI is unconfigured."""
    agent = request.app.state.agent
    if agent is None:
        raise HTTPException(status_code=503, detail="The /ask endpoint requires Azure OpenAI settings (AZURE_OPENAI_*) in the environment.")
    return agent  # type: ignore[no-any-return]


@app.post("/query", response_model=QueryResponse)
async def run_query(payload: QueryRequest, client: Annotated[Neo4jClient, Depends(_get_client)]) -> QueryResponse:
    try:
        columns, records = await client.run_query(payload.query, payload.parameters, payload.database)
    except Neo4jError as exc:
        # Surface Cypher/syntax/constraint errors to the caller as a 400.
        raise HTTPException(status_code=400, detail=exc.message or str(exc)) from exc
    return QueryResponse(columns=columns, records=records, count=len(records))


@app.post("/ask")
async def ask(payload: AskRequest, agent: Annotated[KnowledgeGraphAgent, Depends(_get_agent)]) -> StreamingResponse:
    """Stream the answer as newline-delimited JSON events (metadata, tokens, done).

    The 503-when-unconfigured check happens in ``_get_agent`` before the response
    starts. Any failure after streaming begins is reported as an in-band event
    (``{"type": "error"}``) rather than an HTTP status, since headers are already sent.
    """

    async def event_generator() -> AsyncIterator[bytes]:
        async for event in agent.ask(payload.question):
            yield (json.dumps(event) + "\n").encode("utf-8")

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")
