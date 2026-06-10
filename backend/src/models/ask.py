"""Request model for the ``/ask`` endpoint."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# Accept only plain ISO calendar dates (YYYY-MM-DD). The value is compared as a string
# against versioned nodes' validFrom/validTo, so the format must be exact and ordered.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="A natural-language question about the knowledge graph.")
    user: str | None = Field(
        default=None,
        description="Id of the identity asking the question. Resolved server-side against the access policy into a principal; "
        "an unknown or omitted id falls back to the policy's least-privilege default identity (default-deny).",
    )
    as_of: str | None = Field(
        default=None,
        description="Optional ISO date (YYYY-MM-DD) selecting an as-of snapshot for versioned entities. When omitted, only "
        "the current version of each versioned node is queried; when set, the backend injects a temporal filter so only the "
        "version valid at that date participates. Applies to versioned entities only; unversioned data is unaffected.",
    )

    @field_validator("as_of")
    @classmethod
    def _validate_as_of(cls, value: str | None) -> str | None:
        if value is not None and not _ISO_DATE.match(value):
            raise ValueError("as_of must be an ISO date in YYYY-MM-DD format")
        return value
