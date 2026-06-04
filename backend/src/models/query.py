"""Request/response models for the ``/query`` endpoint."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The Cypher query to execute.")
    parameters: dict[str, Any] = Field(default_factory=dict, description="Named query parameters ($name) referenced by the query.")
    database: str | None = Field(default=None, description="Override the target database (defaults to NEO4J_DATABASE).")


class QueryResponse(BaseModel):
    columns: list[str] = Field(description="Return column names, in order.")
    records: list[dict[str, Any]] = Field(description="Result rows keyed by column name.")
    count: int = Field(description="Number of rows returned.")
