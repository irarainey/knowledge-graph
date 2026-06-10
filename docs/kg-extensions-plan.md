# KG PoC extensions — design plan

Four areas: (1) classification & authorization, (2) versioning, (3) federation,
(4) scalability / external storage. Researched industry approaches + rubber-ducked.

> **Status (2026-06-10):** Phase 0 foundations are **complete** — selected-user identity,
> the **external policy store** (`backend/src/authz/`, `backend/policy/access-policy.json`,
> `/users`, the chat UI identity selector), the **audit log** (`kg.audit`, surfaced in the
> debug panel) and **query-safety limits** (read-only `assert_safe_cypher`, statement
> timeout, row cap). **Area 1 (classification & authorization)** is also implemented via the
> decided **Option C (structured-intent)** strategy: typed query intents, two-dimensional
> capability + clearance enforcement in `query_builder.py`, **clearance-gated categories**
> (e.g. `route` for the maintenance engineer — visible on unclassified flights, nulled on
> classified ones), and post-retrieval redaction. **Area 2 (versioning)** — valid-time
> `Specification` versions and event-dated `Flight` as-of queries plus ontology versioning —
> is implemented. Areas 3–4 (federation, scalability) remain future work. See the decision
> section below for the enforcement rationale.

## Hard constraints (shape every decision)
- **Neo4j Community Edition**: NO native RBAC, NO label/property security, NO multiple
  databases, NO Fabric/composite DBs. Those are Enterprise. ⇒ enforcement lives in the
  **FastAPI backend**, not the database.
- **Text-to-Cypher**: the LLM emits arbitrary read Cypher over the whole graph today.
  This is the riskiest interaction. The LLM cannot be trusted to enforce policy.

## The governing principle (the trust boundary)
The backend is the security boundary. The pipeline must be:

```
user identity → backend policy → user-scoped schema/tools/query constraints
   → DB query → result sanitizer → answer LLM
```

The LLM must NEVER see unauthorized schema or unauthorized raw rows. **Default-deny.**
Post-retrieval redaction is defense-in-depth, NOT the primary control — it leaks via
aggregates (COUNT/AVG/MIN/MAX), existence checks, path counts, error/empty differences,
schema example values, and cross-turn conversation memory.

**Honest framing for the PoC:** "application-level, demonstrative" — not enterprise ABAC,
not database federation. Say so in the docs.

---

## Phase 0 — Cross-cutting foundations (build FIRST; everything depends on these)
1. **Selected-user identity**: Streamlit user dropdown → sent to backend, resolved
   server-side to attributes/clearance. NOT authentication (state that). Conversation
   state scoped per user; **reset chat on user switch** (prevents memory-leak attacks).
2. **External policy store** (config/policy code, versioned separately from the graph):
   user attributes & clearance, per-label/property visibility, aggregate-disclosure
   rules, domain-ownership rules. Default-deny. Classification *metadata* may live in the
   graph (it's part of the data model); the *enforcement policy* does not.
3. **Audit log** (per request): selected user + attributes, NL question, generated Cypher,
   schema version shown, policy version, rows returned, fields redacted, clauses denied,
   answer source IDs. Surface a slice in the existing debug panel.
4. **Query safety** (beyond EXPLAIN read-only): statement timeout, row LIMIT cap,
   traversal-depth cap, block `CALL`/procedures/`LOAD CSV`/schema-introspection, block
   unsafe aggregates over sensitive labels, deterministic LIMIT, query logging.

---

## Area 1 — Classification & Authorization (centerpiece, highest risk)
**Data model:** node/relationship `classification` + `securityLabels: [...]`; a property-
level sensitivity map (which property keys on which labels are sensitive). Example:
Flight.blockTime_minutes (duration) = OK for maintenance; Flight.departureAerodrome /
destinationAerodrome / distance_nm + DEPARTS_FROM/ARRIVES_AT/OCCURS_AT = restricted;
whole military flights = classified.

**Enforcement (layered, deterministic, outside the LLM):**
- (a) **User-scoped schema prompt** — strip unauthorized labels, relationship types,
  property names, AND example values before Text2Cypher sees them (property *names* like
  `militaryMissionCode` leak by existence).
- (b) **Pre-query scoping** — the key fix for aggregate/inference leakage: unauthorized
  nodes/properties must not participate in execution. Options (decision needed, see below):
  restricted Cypher subset; OR structured-intent → backend builds Cypher; OR query only a
  user-scoped sanitized projection/subgraph.
- (c) **Post-retrieval redaction** — drop unauthorized nodes, null unauthorized props
  (defense-in-depth only).
- (d) **Answer-layer isolation** — answer LLM sees only sanitized rows.

**Demo slice:** 3 users (`public`, `maintenance_engineer`, `restricted_ops`) + a small
real policy matrix (duration vs route vs military vs maintenance components). Show:
user-specific schema, guard rejecting unsafe aggregates, deterministic pre-filtering,
post-row redaction, audit entry.

**Adversarial tests (more convincing than features):** direct restricted-property request,
aggregate over restricted flights, existence query, path-count query, schema leakage,
user-switch memory leak.

**Risk to avoid:** claiming arbitrary Text2Cypher + redaction is secure. If unauthorized
data participates in LLM-generated Cypher execution, it is not secure even if displayed
rows are redacted.

---

## Area 2 — Versioning (ontology / nodes)
**Keep narrow.** Bitemporal everywhere will wreck Text2Cypher reliability (the LLM forgets
validFrom/validTo/current filters → mixes current & historical facts, which is worse than
no versioning).

**Data model:** `logicalId`, `version`, `validFrom`, `validTo`, `current`,
`PREVIOUS_VERSION` for **1–2 entity types only** (Specification, Document, maybe Component)
**plus their relevant relationships** (versioning nodes but not edges gives wrong history).

**Two query modes:**
- **Current** (default): LLM only sees current schema/data.
- **As-of** (user picks a date): backend applies the temporal filter deterministically —
  NOT the LLM.

**Ingest validation:** exactly one `current=true` per `logicalId` (idempotent import can
otherwise create 0 or many).

**Ontology versioning:** semantic version + deprecate-don't-delete (`owl:deprecated`-style);
include ontology/schema version in retrieval context; one active version + deprecated terms;
don't mix ontology versions in one schema prompt unless intentionally demoing migration.

**Authz × versioning rule (pick one explicitly):** "authorization evaluated using *current*
policy against the selected as-of data snapshot." Simpler and defensible.

---

## Area 3 — Federation
**Honest naming: "application-level federated retrieval," not DB federation** (CE has no
Fabric). Agentic per-domain tools are demo-friendly but only real federation if you also
handle identity resolution, dedup, cross-domain joins, policy harmonization, provenance,
partial failures, conflicting facts, freshness — call that out.

**Demo slice:** one Neo4j instance + `domain`/`owner` metadata; two domain-scoped tools
(`search_flight_ops_graph`, `search_maintenance_graph`), each applying its own schema
filter + authz. Agent calls both for one cross-domain question (e.g. "Was the aircraft
flown after a maintenance-relevant component change?"), merges sanitized summaries, reports
provenance. Prefer a small fixed router over a general one.

**Federation × authz:** apply authz BEFORE merge; second policy check AFTER merge (merged
fields can leak indirectly, e.g. duration + weather/ATC ⇒ inferred route). Preserve
per-field security labels through the merge.

**Federation × versioning / provenance:** each returned row carries source domain, source
system, data version, ontology version, retrieval time, policy applied. Show in the debug
panel.

---

## Area 4 — Scalability / external storage
**Externalize Document content first** (Documents already exist, are naturally large,
demonstrate graph-as-index cleanly). Telemetry (FlightPhase) stays in-graph as summaries;
note external time-series store as a design note, don't build it (would look contrived at
current scale).

**Data model:** `Document` keeps `documentId` (opaque), `title`, `contentType`, `checksum`,
`version`, `classification`, `storageRef` = **opaque internal ID, never a URL**. Content
lives in Azure Blob.

**Backend-mediated authorized fetch:** authz check → fetch blob (managed identity /
short-lived signed access) → verify checksum/version → return only allowed excerpt.
**Never expose blob URIs to the LLM** (bypass risk, names leak facts, SAS URLs in
logs/history). Sanitize/isolate document content before the LLM (prompt-injection from
document text).

**Externalization expands the security boundary:** the blob must enforce the same-or-
stricter policy as the pointer; separate audit logs for graph access vs blob access.

---

## Sequencing (dependency order)
1. **Phase 0 foundations** (identity, policy store, audit, query safety) — prerequisite.
2. **Authorization** — depends on Phase 0; the core deliverable.
3. **Versioning** — depends on Phase 0; mostly independent, interacts with authz.
4. **External storage** — depends on authz (fetch mediation).
5. **Federation** — depends on authz + provenance; ties everything together.

## Out of scope / honest limitations (document these)
- Client-side user dropdown is not authentication.
- App-layer enforcement is demonstrative, not enterprise RBAC/ABAC.
- "Federation" is application-level retrieval, not Neo4j Fabric.
- Versioning is narrow (1–2 entity types), not full bitemporal graph versioning.

## Decided: Authorization enforcement strategy = **C (structured-intent)**
The LLM emits a **typed query object** (entity, filters, fields, aggregate); the backend
**validates it against policy** (entity/field/aggregate allowed for this user?) and
**deterministically builds parameterized Cypher**. Rationale: converts security from
"constrain an arbitrary language" (open-ended, fragile) into "validate a small typed
surface" (closed, testable); fits MAF typed tools + the repo's separation-of-concerns;
makes versioning (as-of) and federation (per-domain tools) layer on cleanly.

**Architectural change this implies (vs today's Text2Cypher):**
- Replace the free-form `Text2CypherRetriever` data path with a **policy-aware query
  builder**. The MAF tool's typed arguments become the intent surface (entity + filters +
  fields + optional aggregate). The agent maps NL → typed tool args; the backend owns
  Cypher generation, parameterisation, policy checks, and execution.
- **Keep the debug panel showing the generated Cypher** — now backend-built, so it's
  trustworthy *and* still demonstrates "question → graph query".
- Aggregates are an explicit, gated field on the intent (deny by default for sensitive
  entities) — closes the COUNT/AVG/path-count leakage channel structurally.
- No general Cypher parsing/rewriting (avoids the rabbit hole). No hybrid free-form path
  (would reopen every leak channel).
- The existing relevance guardrail stays (off-topic gate); authz is separate and runs
  inside the query builder. neo4j-graphrag may still be used for the *schema introspection*
  used to design the intent surface, but not to execute arbitrary LLM Cypher.

Versioning & federation both ride on this: as-of becomes an injected temporal filter in
the builder; per-domain federation becomes per-domain typed tools each with their own
policy + schema scope.

