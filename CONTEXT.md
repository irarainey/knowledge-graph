# CONTEXT.md

## What this is
A proof-of-concept knowledge graph for a Cessna 172S (G-ECHO): an operational
aircraft graph plus an SDLC/engineering overlay, in Neo4j. Two front ends
(Vue graph renderer, Streamlit chat UI) and a FastAPI backend that answers
natural-language questions via a Microsoft Agent Framework agent backed by
Azure OpenAI.

## Current state
- Operational aircraft graph: implemented, queryable via `/query` (raw Cypher)
  and `/ask` (NL, policy-scoped).
- SDLC/engineering overlay: implemented (added 2026-06-17), joined to the
  aircraft graph by cross-domain edges, queryable via the `software_engineer`
  identity.
- Authorization (capability grants + row-level clearance): implemented.
- Versioning (valid-time + event-time, narrow scope): implemented.
- External document storage (Area 4 of the extensions plan): implemented.
- Federation (Area 3 of the extensions plan): **not built** — future work.

## Goals
- Demonstrate KG-augmented RAG where the LLM never writes raw Cypher and
  never sees unauthorized schema or rows.
- Demonstrate an application-layer authorization model on Neo4j Community
  Edition (no native RBAC).
- Demonstrate narrow, deterministic versioning (as-of queries) without
  full bitemporal modelling.

## Non-goals
- Production authentication (identity is UI-selected, not verified).
- Enterprise-grade ABAC/RBAC enforced in the database.
- Full bitemporal graph versioning.
- True multi-database federation (Neo4j Fabric or equivalent).

## Constraints
Anything tagged `CRITICAL` is binding — stop and flag if a change would
contradict it, rather than working around it.

- `CRITICAL` — The backend is the sole authorization trust boundary. The LLM
  is untrusted. Default-deny: an identity sees nothing not explicitly
  granted. No unauthorized schema, field name, or row may reach the LLM.
  See [ADR-0002](docs/adr/0002-application-layer-authorization-boundary.md).
- `CRITICAL` — The LLM never writes or sees raw Cypher. It emits a typed
  query intent; the backend deterministically builds parameterised Cypher
  from it. See [ADR-0001](docs/adr/0001-structured-intent-query-builder.md).
- `CRITICAL` — Runs on stock Neo4j Community Edition. No RBAC, no
  label/property security, no multiple databases, no Fabric/composite DBs.
  Do not assume or design around Enterprise-only features.
- `CRITICAL` — Document bodies are never exposed to the LLM by reference.
  The `storageRef`/blob URI must never reach the model, logs, or audit
  output. See [ADR-0006](docs/adr/0006-externalized-document-storage.md).
- `CRITICAL` — A clearance-gated category redacts a classified row's fields
  field-by-field instead of hiding the whole row. This is opt-in and
  off by default: never grant it, or widen what it exposes, without an
  explicit per-identity need-to-know justification.
  See [ADR-0004](docs/adr/0004-clearance-gated-categories.md).
- `CRITICAL` — Identity is client-selected, not authenticated. Never treat
  the `user` field as a verified claim, and always reset conversation state
  on identity switch (prevents cross-identity memory leakage).
  See [ADR-0007](docs/adr/0007-selected-identity-not-authentication.md).
- `CRITICAL` — Frontend package management is pnpm only — never npm or yarn.
- `IMPORTANT` — This is a local-running PoC only. No Neo4j Enterprise
  licence — Community Edition is the only option in scope, not a
  deliberate trade-off against Enterprise.
- `IMPORTANT` — Several design choices (e.g. policy file format) were not
  formally evaluated against alternatives; they were the simplest workable
  option for a PoC. Don't assume unstated rigor behind them — see the ADRs'
  "Rejected alternatives" sections before changing one.

## Load on demand (do not load unless the task needs it)
- `IMPORTANT` — [ARCHITECTURE.md](ARCHITECTURE.md) — component map,
  data-flow, request pipeline. Load when a task touches component
  boundaries or the request pipeline.
- `IMPORTANT` — [DECISIONS.md](DECISIONS.md) — index of decisions with ADR
  links. Load when a task might touch an existing decision.
- `REFERENCE` — `docs/adr/*` — full reasoning for individual decisions.
  Load the specific ADR only when a task touches that decision.
- `IMPORTANT` — [backend/README.md](backend/README.md) — implementation
  detail: authorization model, versioning, query safety/audit, evaluation,
  Neo4j CE trade-offs. Load when working in the backend.
- `REFERENCE` — [docs/sdlc-knowledge-graph-guide.md](docs/sdlc-knowledge-graph-guide.md) —
  SDLC/engineering domain model (node types, edge semantics, V-model
  mapping). Load when working on the SDLC overlay.
- `REFERENCE` — [docs/kg-extensions-plan.md](docs/kg-extensions-plan.md) —
  design rationale and remaining future work (federation). Load only when
  designing a new extension area.
