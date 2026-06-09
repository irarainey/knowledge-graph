"""Request model for the ``/ask`` endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="A natural-language question about the knowledge graph.")
    user: str | None = Field(
        default=None,
        description="Id of the identity asking the question. Resolved server-side against the access policy into a principal; "
        "an unknown or omitted id falls back to the policy's least-privilege default identity (default-deny).",
    )
