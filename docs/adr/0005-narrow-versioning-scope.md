# ADR-0005: Narrow versioning scope, not full bitemporal modelling

Date: pre-2026-06-10 (exact commit not isolated in git history)
Status: Accepted

## Context
The plan considered versioning the whole graph bitemporally (validFrom/
validTo/current on every entity and relationship).

## Decision
Versioning is scoped to 1–2 entity types (Specification; Document/Component
noted as candidates) plus their relevant relationships, using two distinct
temporal concepts kept separate: valid-time versioning (a logical entity is
revised) and event time (an event happened or didn't yet). Both are driven
by one as-of date, injected deterministically by the backend.

## Rejected alternative
Full bitemporal versioning across the whole graph. The plan's stated reason:
"Bitemporal everywhere will wreck Text2Cypher reliability (the LLM forgets
validFrom/validTo/current filters → mixes current & historical facts)."

Note: this rationale predates the later replacement of free-form Text2Cypher
with the structured-intent query builder (ADR-0001), where the backend —
not the LLM — injects temporal filters deterministically. This is a local
PoC for testing ideas; the narrow-scope decision has not been formally
revisited since that replacement, and no re-evaluation is currently
planned.

## Consequences
- As-of queries are only meaningful for the versioned entity types.
- Import validates "at most one `current=true` per `logicalId`" — enforced
  in the import script, not the database (Community Edition has no
  existence/node-key constraints).
