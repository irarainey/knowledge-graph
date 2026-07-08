# ADR-0001: Structured query-intent instead of LLM-generated Cypher

Date: 2026-06-09
Status: Accepted

## Context
The original design had the LLM emit arbitrary read Cypher over the whole
graph. This was identified as the riskiest interaction: an untrusted LLM
cannot be relied on to enforce access policy in the Cypher it writes.

## Decision
The LLM emits a typed query intent (entity, filters, fields, optional
aggregate). The backend validates the intent against policy and
deterministically builds parameterised Cypher — the LLM never writes or
sees Cypher.

## Rejected alternatives
The plan names two other options considered for constraining LLM-driven
queries: a restricted Cypher subset, and querying only a pre-built,
user-scoped sanitized projection/subgraph. The repository states why
structured-intent (C) was preferred over free-form generation in general
(converts security from "constrain an arbitrary language" into "validate a
small typed surface"; fits typed agent-framework tools; layers cleanly onto
versioning/federation) but does not document the specific reasons the other
two named options were rejected. This is a local PoC used to test ideas —
no formal comparison of the three options was carried out beyond the
stated preference for C.

## Consequences
- No general Cypher parsing/rewriting layer needed.
- Aggregates become an explicit, gated field on the intent.
- The debug panel still shows generated Cypher (now backend-built, so
  trustworthy).
