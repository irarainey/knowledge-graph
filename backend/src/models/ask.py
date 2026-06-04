"""Request/response models for the ``/ask`` endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="A natural-language question about the knowledge graph.")


class AskResponse(BaseModel):
    answer: str = Field(description="The agent's natural-language answer.")
    cypher_used: list[str] = Field(description="The Cypher queries the agent ran to find the answer.")
    records: list[dict[str, Any]] = Field(description="The graph rows the agent retrieved as context.")
