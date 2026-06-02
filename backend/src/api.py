"""FastAPI service exposing the Neo4j knowledge graph over HTTP.

A single ``POST /query`` endpoint runs an arbitrary Cypher query (with optional
parameters) supplied in the request body and returns the resulting records.
Connection details come from the environment / ``backend/.env``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from neo4j.exceptions import Neo4jError
from pydantic import BaseModel, Field

from neo4j_client import Neo4jClient, Neo4jSettings, load_env


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The Cypher query to execute.")
    parameters: dict[str, Any] = Field(default_factory=dict, description="Named query parameters ($name) referenced by the query.")
    database: str | None = Field(default=None, description="Override the target database (defaults to NEO4J_DATABASE).")


class QueryResponse(BaseModel):
    columns: list[str] = Field(description="Return column names, in order.")
    records: list[dict[str, Any]] = Field(description="Result rows keyed by column name.")
    count: int = Field(description="Number of rows returned.")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the shared Neo4j driver on startup and close it on shutdown."""
    load_env()
    client = Neo4jClient(Neo4jSettings.from_env())
    await client.verify_connectivity()
    app.state.neo4j = client
    try:
        yield
    finally:
        await client.close()


app = FastAPI(
    title="Knowledge Graph API",
    description="Query the Neo4j knowledge graph with arbitrary Cypher.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def get_client(request: Request) -> Neo4jClient:
    """Return the shared Neo4j client created during app startup."""
    return request.app.state.neo4j  # type: ignore[no-any-return]


@app.post("/query", response_model=QueryResponse)
async def run_query(payload: QueryRequest, client: Annotated[Neo4jClient, Depends(get_client)]) -> QueryResponse:
    try:
        columns, records = await client.run_query(payload.query, payload.parameters, payload.database)
    except Neo4jError as exc:
        # Surface Cypher/syntax/constraint errors to the caller as a 400.
        raise HTTPException(status_code=400, detail=exc.message or str(exc)) from exc
    return QueryResponse(columns=columns, records=records, count=len(records))
