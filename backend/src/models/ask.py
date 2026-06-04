"""Request model for the ``/ask/stream`` endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="A natural-language question about the knowledge graph.")
