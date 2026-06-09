"""Per-request audit trail for the ``/ask`` pipeline.

Every answered, refused or failed question produces one :class:`AuditRecord`: who asked
(the resolved principal and policy version), what they asked, what Cypher ran, how much
came back, and any query-safety denials. The record is written to a dedicated ``kg.audit``
logger (so it can be routed/retained independently, and is exported to Application
Insights when telemetry is configured) and a compact copy is returned for the debug panel.

The audit trail is part of the authorization story: it makes every answer attributable to
an identity and a policy version, alongside the redacted fields and answer-source ids that
the same record carries.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from authz import Principal
from common.logging_config import get_logger

logger = get_logger("audit")

# Audit outcomes.
OUTCOME_ANSWERED = "answered"
OUTCOME_REFUSED_OFF_TOPIC = "refused_off_topic"
OUTCOME_ERROR = "error"


def schema_fingerprint(schema_text: str) -> str:
    """Short, stable fingerprint of the schema shown to the LLM (detects drift in audit)."""
    return hashlib.sha256(schema_text.encode("utf-8")).hexdigest()[:12]


class AuditRecord(BaseModel):
    """A single auditable ``/ask`` request outcome."""

    timestamp: str = Field(description="UTC ISO-8601 time the request was recorded.")
    outcome: str = Field(description="answered | refused_off_topic | error.")
    user: str | None = Field(default=None, description="Acting identity id.")
    role: str | None = Field(default=None)
    clearance: str | None = Field(default=None)
    policyVersion: str | None = Field(default=None, description="Access policy version the principal was resolved under.")
    schemaFingerprint: str | None = Field(default=None, description="Fingerprint of the schema shown to the LLM.")
    question: str = Field(description="The natural-language question asked.")
    cypher: list[str] = Field(default_factory=list, description="Cypher statement(s) actually run.")
    recordCount: int = Field(default=0, description="Number of rows returned to the answer step.")
    llmCalls: int = Field(default=0)
    denied: list[str] = Field(default_factory=list, description="Query-safety denials raised during the request.")
    durationMs: float = Field(default=0.0)


def build_audit(
    *,
    principal: Principal | None,
    question: str,
    outcome: str,
    schema_fingerprint: str | None,
    cypher: list[str],
    record_count: int,
    llm_calls: int,
    denied: list[str],
    duration_ms: float,
) -> AuditRecord:
    """Assemble an :class:`AuditRecord` from the request's outcome and telemetry."""
    return AuditRecord(
        timestamp=datetime.now(UTC).isoformat(timespec="milliseconds"),
        outcome=outcome,
        user=principal.id if principal is not None else None,
        role=principal.role if principal is not None else None,
        clearance=principal.clearance if principal is not None else None,
        policyVersion=principal.policyVersion if principal is not None else None,
        schemaFingerprint=schema_fingerprint,
        question=question,
        cypher=cypher,
        recordCount=record_count,
        llmCalls=llm_calls,
        denied=denied,
        durationMs=duration_ms,
    )


def log_audit(record: AuditRecord) -> dict[str, Any]:
    """Write ``record`` to the audit log and return it as a dict for the debug panel."""
    payload = record.model_dump()
    logger.info("audit %s", record.model_dump_json())
    return payload
