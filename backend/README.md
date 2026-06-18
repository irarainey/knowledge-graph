# Backend

Python FastAPI service for the Knowledge Graph application. Provides API endpoints that query a Neo4j graph database for knowledge retrieval, then use Azure OpenAI to generate natural-language answers (knowledge RAG).

## Setup

```bash
cd backend
uv sync
```

## Development

All tasks use [poethepoet](https://poethepoet.naber.dev/) via `uv run poe`:

```bash
uv run poe lint          # ruff check + format check + mypy
uv run poe format        # auto-fix lint issues and format code
uv run poe test          # run all tests
uv run poe evaluate      # offline evaluation of /ask (see "Evaluation" below)
```

Run a single test:

```bash
uv run pytest tests/test_example.py::test_name -v
```

## Importing the knowledge graph into Neo4j

`scripts/import_graph.py` loads `data/aircraft-knowledge-graph.json` into a running Neo4j
instance. It reads connection settings from `backend/.env` (copy `.env.example`
to get started):

```bash
cp .env.example .env
```

| Variable | Default | Description |
| --- | --- | --- |
| `NEO4J_URI` | `bolt://localhost:7687` | Bolt connection URI |
| `NEO4J_USERNAME` | `neo4j` | Username |
| `NEO4J_PASSWORD` | `password` | Password |
| `NEO4J_DATABASE` | `neo4j` | Target database |

Run the import:

```bash
uv run poe import-graph            # update / upsert (idempotent)
uv run poe import-graph --clear    # delete everything first, then import
```

To also load the **engineering / SDLC overlay** (`data/sdlc-knowledge-graph.json` —
requirements, implementation, verification, assurance, safety, configuration and work
management for the aircraft's software), import it *after* the aircraft graph so its
cross-domain edges (e.g. a release `INSTALLED_ON` the aircraft) resolve:

```bash
uv run poe import-graph --clear    # 1. aircraft (operational) graph first
uv run poe import-sdlc             # 2. then the SDLC overlay (no --clear)
```

Or do both in the correct order with one command:

```bash
uv run poe import-all              # wipe, import aircraft, then import SDLC overlay
```

Order matters: relationships are upserted with `MATCH (a {id})...MATCH (b {id})`, so a
cross-domain edge is silently skipped if its aircraft endpoint is not already present. The
two datasets share no node ids, so no namespacing is needed. The SDLC entities are
queryable via `/ask` by the `software_engineer` identity (see the access policy below).

The import upserts by default — nodes are matched on their `id` property and
relationships on the (start node, type, end node) triple — so re-running is safe.
Because upsert only adds and overwrites, use `--clear` after removing or renaming
nodes/relationships to get a clean reload. Labels and relationship types cannot be
parameterised in Cypher, so they are identifier-sanitised and interpolated; all other
values are passed as parameters. The import uses plain Cypher only (no APOC), so it works
against a stock Community container.

**Classification overlay.** Node sensitivity is *not* carried in the shared
`data/aircraft-knowledge-graph.json` export (which also feeds the public Vue renderer). Instead it
is a **backend-owned overlay**, [`policy/graph-classification.json`](policy/graph-classification.json),
applied as a second pass after the graph is imported: it sets a `classification` property
(and `securityLabels`) on the listed node ids, which the row-level clearance filter then
gates against (see [Authorization model](#authorization-model)). Keeping it beside the
access policy — separate from the graph data — means *who-may-see-what* and *what-is-sensitive*
are both versioned independently of the graph itself. Override its location with
`--classification`; nodes not listed are imported **without** a classification (and so,
in this PoC, are treated as unclassified — see the
[Community Edition trade-offs](#neo4j-community-edition-trade-offs-and-enterprise-alternatives)
for why existence constraints would harden this).

Additional flags:

- `--file <path>` — import a different JSON export (defaults to `data/aircraft-knowledge-graph.json`).
- `--env-file <path>` — load Neo4j settings from a specific `.env` file.
- `--classification <path>` — use a different classification overlay (defaults to `policy/graph-classification.json`).

A convenience wrapper, `scripts/import-data.sh`, runs the same command from any
directory and forwards these arguments.

**Version validation.** After loading, the import asserts the temporal invariant that makes
the "current" query mode well-defined: every versioned `logicalId` must have **at most one**
`current=true` version and **unique** `version` numbers. The import fails loudly if not (zero
current versions is allowed — a fully retired logical entity). See
[Versioning](#versioning-temporal-data-and-ontology) for the data model and why this is
enforced in the importer rather than the database on Community Edition.

## Querying the graph (REST API)

`src/app.py` is a FastAPI service that runs arbitrary Cypher queries against the
Neo4j database. It reads the same `backend/.env` connection settings as the
import script and opens a single shared driver for the lifetime of the process.

Start the server:

```bash
uv run poe serve        # http://localhost:8080, autoreload enabled
```

Interactive API docs are available at `http://localhost:8080/docs`.

### `POST /query`

Request body:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `query` | string | yes | The Cypher query to execute. |
| `parameters` | object | no | Named parameters referenced as `$name` in the query. |
| `database` | string | no | Override the target database (defaults to `NEO4J_DATABASE`). |

Response body:

| Field | Type | Description |
| --- | --- | --- |
| `columns` | string[] | Return column names, in order. |
| `records` | object[] | Result rows keyed by column name. |
| `count` | integer | Number of rows returned. |

Nodes and relationships are returned as their property maps; temporal values are
serialised as ISO-8601 strings. A Cypher/syntax error returns HTTP `400` with the
Neo4j message in `detail`; an empty `query` returns HTTP `422`.

Example:

```bash
curl -X POST http://localhost:8080/query \
  -H 'Content-Type: application/json' \
  -d '{"query": "MATCH (f:Flight) RETURN f.name AS flight, f.flightTime_hours AS hours ORDER BY hours DESC LIMIT 3"}'
```

```json
{
  "columns": ["flight", "hours"],
  "records": [{"flight": "Flight 001", "hours": 0.8}],
  "count": 1
}
```

> ⚠️ **This endpoint bypasses the authorization trust boundary entirely.** It executes any
> Cypher it is given (including writes/deletes) against the raw graph — **no policy, no
> clearance filter, no redaction**. It exists as a developer/debug escape hatch for the
> PoC; the authorization model in this document applies only to the natural-language
> [`/ask`](#post-ask) path. Do not expose `/query` publicly without adding authentication
> and query restrictions (or removing it).

## Natural-language questions (agentic MAF + structured-intent query tool)

`src/agents/knowledge_graph_agent.py` answers natural-language questions over the graph
with a single [**Microsoft Agent Framework**](https://github.com/microsoft/agent-framework)
`Agent` that owns orchestration and is given **two typed tools**: `query_knowledge_graph`
and `fetch_document_content`. For graph questions the agent does **not** write Cypher.
Instead it emits a structured *query intent* — an entity (node label), the fields to return,
optional filters, and an optional aggregate. The backend validates that intent against the
acting identity's access policy and **deterministically builds and runs** a parameterised,
read-only Cypher query (`src/authz/query_builder.py`). For questions about what a **document**
says (the POH, a maintenance manual, an airworthiness directive…) the agent calls
`fetch_document_content`, which returns an authorised, integrity-checked excerpt of a document
body that lives **outside** the graph (see [External document storage](#external-document-storage-area-4)).
The agent is **required** to call a tool on its first turn — it chooses which — then writes
the final answer from what it gets back.

Putting query construction in the backend — not the LLM — is the authorization
**enforcement** boundary. Unauthorised entities, fields and aggregates are
rejected before execution, and a row-level **clearance filter** is always injected so
classified nodes never participate in a query (not even in a `count` or existence check)
for an under-cleared identity. The same two-dimensional check (entity + sensitivity
category + clearance) gates document bodies. The agent's instructions and tool surface are
scoped per request to only the entities and fields the identity may see, so unauthorised
field *names* never reach the model. Read-only execution is additionally guaranteed by
`assert_safe_cypher` (`common/query_safety.py`), so the model can never modify the graph.

Forcing a tool call preserves the grounding guarantee of a deterministic retrieve→generate
pipeline (the agent only ever answers from data it actually retrieved) while keeping
orchestration in native MAF, making it straightforward to add further tools later.

Before any retrieval or LLM call, a **deterministic relevance guardrail** (`common/guardrails.py`,
**no extra LLM call**) rejects off-topic questions. Off-topic questions return a fixed
refusal message with empty metadata and zero LLM usage.

The graph schema is introspected with plain Cypher (no APOC required), so it works
against a stock Neo4j Community container. The schema is read once at startup —
**restart the backend after re-importing the graph** so the agent picks up changes.

### How it works

A request to `/ask` flows through the relevance guardrail, then a single MAF agent that
is required to call a tool once before answering — either the graph **query** tool (shown
below) or the **document** tool (see
[External document storage](#external-document-storage-area-4)):

```
question
   │
   ▼
┌──────────────────────── Relevance guardrail (no LLM) ───────────────────────┐
│ Tokenise the question and intersect it with a vocabulary built from the     │
│ live graph schema + curated domain keywords. Off-topic → fixed refusal,     │
│ empty metadata, zero LLM usage. On-topic → continue.                         │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                    ▼
┌──────────────────────────────── MAF Agent (scoped per identity) ────────────┐
│ Turn 1 — LLM call #1 (planning): required to call a tool; the model chooses  │
│   query_knowledge_graph (a typed intent) or fetch_document_content (doc ref). │
│   ┌──────────────── Backend query builder (NO LLM) ────────────────────────┐ │
│   │ Validate intent vs policy (entity/field/aggregate gates) → build a       │ │
│   │ parameterised, read-only Cypher query with an injected clearance filter  │ │
│   │ → assert_safe_cypher → run (READ routing) → redact → rows as JSON.       │ │
│   └─────────────────────────────────────────────────────────────────────────┘ │
│   (a document question instead authorizes + fetches an excerpt — see below.)  │
│ Turn 2 — LLM call #2 (answer): tool choice auto; the agent writes the final  │
│ answer from the rows / excerpt (delivered as the tool-result message).       │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                    ▼
              NDJSON stream { metadata, token…, stats, done }
```

A single on-topic question therefore makes **two** LLM calls — the agent's planning turn
and the answer turn — both surfaced individually in the `stats` event (see
[`POST /ask`](#post-ask)). There is **no cypher-generation LLM call**: turning the intent
into Cypher is deterministic.

Step by step, in `src/agents/knowledge_graph_agent.py`:

1. **Startup (`KnowledgeGraphAgent.from_settings`).** The agent opens its own
   **synchronous** Neo4j driver (separate from the async driver that backs `/query`),
   builds the Microsoft Agent Framework chat client (`build_chat_client`), introspects the
   schema once (`fetch_schema_text`) to fingerprint it (for audit drift detection) and to
   build the guardrail vocabulary (`build_relevance_vocabulary`), and receives the
   `PolicyStore` so the query builder can enforce authorization. Query safety
   (`_install_query_safety`) wraps the driver so every statement is re-validated read-only
   with a per-statement timeout.

2. **Relevance guardrail (`common/guardrails.py`).** When a question arrives, it is first
   checked against a vocabulary derived from the live schema (label/relationship/property
   tokens, CamelCase-split and singularised) plus curated domain keywords, minus a
   denylist of generic tokens (`name`, `type`, `date`, …). If no question token matches,
   the request is **refused without any LLM call** — a fixed message is streamed with
   empty `metadata` and zero token usage. This is a relevance gate, **not** a
   prompt-injection defence.

3. **Per-request scoped agent (`_build_maf_agent`).** The MAF `Agent` is built for each
   request so its system instructions embed `PolicyStore.describe_surface(principal)` — a
   compact catalogue of *only* the entities and fields the acting identity may see — and
   its tool is bound (via closure) to that principal. The agent's `default_options` pin the
   tool choice to `required` so retrieval is forced on the first turn.

4. **Structured intent + deterministic query build (`authz/query_builder.py`).** The
   forced `query_knowledge_graph` tool receives the model's typed intent and calls
   `build_query`, which enforces, in order: an **entity gate** (the entity must be granted),
   a **field gate** (every projected *and* filtered field must be visible to the
   principal), an **aggregate gate** (aggregates need an explicit grant and a visible target
   field), and (by default) an injected **row-level clearance filter**
   (`n.classification IS NULL OR n.classification IN $__authz_classifications`). For an
   identity with a **clearance-gated** category that whole-row filter is replaced by per-field
   `CASE WHEN` redaction plus clearance guards on gated-field filters and aggregates, so
   classified rows stay visible with only the gated fields nulled (see
   [Clearance-gated categories](#clearance-gated-categories)). Values are
   always parameterised; labels/fields come only from the controlled catalogue and are
   identifier-validated before interpolation. String comparisons (`=`, `<>`, `CONTAINS`,
   `STARTS WITH`, `ENDS WITH`) are **case-insensitive** — both operands are wrapped in
   `toLower(...)` — so a question's casing need not match the stored value exactly (e.g.
   "flight control" still matches "Flight Control Requirement"); ordering comparisons and
   non-string values are emitted verbatim. A denied intent records an audit denial and
   returns a refusal string the agent relays — it never raises a 500.

5. **Read-only enforcement.** The built query is re-checked by `assert_safe_cypher`
   (rejecting writes, `CALL`, multiple statements, restricted namespaces) and executed with
   READ routing under the query-safety timeout. This is defence-in-depth on top of the
   builder only ever emitting `MATCH … RETURN`.

6. **Redaction (`redact_records`) and name enrichment.** Returned rows are stripped to the
   projected fields as a safety net, so even an unexpected key (e.g. `classification` itself)
   never reaches the answer LLM. For a clearance-gated category the gated fields were already
   nulled per-row in the projection. After redaction, aerodrome **code** fields
   (`departureAerodrome`/`destinationAerodrome`) are enriched in place with a sibling
   `<field>Name` resolved from a cached ICAO→name map (`attach_aerodrome_names`), so the answer
   model receives both code and name from the **single** retrieval — no extra LLM round-trip to
   look names up. Because enrichment runs *after* redaction, a code already nulled on a
   classified row yields a null name too (the redaction is inherited, never bypassed). (If the
   model instead requests a synthetic `departureAerodromeName`/`destinationAerodromeName` field
   in its intent, the builder canonicalises it back to the underlying code field so the query
   builds rather than being denied; the name is still attached only here, after retrieval.)
   Rows are capped (`QUERY_ROW_CAP`). If execution fails or returns no rows, the tool returns a
   graceful message and the agent still answers (with empty `metadata`).

7. **Answer generation.** Once MAF resets the forced tool choice to `auto` after the first
   iteration, the agent makes its second turn (LLM call #2) and writes the answer from the
   tool's rows. Its system prompt (`STRUCTURED_AGENT_SYSTEM_PROMPT`) instructs it to always
   call the tool, choose only entities/fields from the scoped catalogue, answer **only**
   from the retrieved rows, say so when the data has no answer (or the request was refused
   by policy), report numbers **exactly**, and reply in **plain text**. It is also told, when
   filtering on a name/title field, to use only the distinctive name (not append the
   entity-type noun such as "baseline"/"claim"), to rely on the case-insensitive matching
   above, and to prefer `CONTAINS` when unsure of the exact stored name.

8. **Response assembly (`ask`).** The synchronous query inside the tool runs in a worker
   thread (`asyncio.to_thread`) so the FastAPI event loop stays responsive. The built Cypher
   and rows are captured via a per-request `retrieval_sink` and emitted up front as a
   `metadata` event, then the MAF agent is streamed natively (`request_agent.run(...,
   stream=True)`) so answer tokens are forwarded as they arrive, followed by a `stats` and a
   `done` event. A `_MafTurnRecorder` chat middleware records each of the agent's two LLM
   turns (planning + answer) individually — MAF otherwise aggregates their token usage — so
   the `stats` event can report both calls separately. If a generation/network error occurs,
   `/ask` **degrades gracefully** — it emits an in-band `error` event instead of a 500.

Both LLM calls (planning and answer) target the same Azure OpenAI deployment via the
Microsoft Agent Framework chat client.

### Cross-domain traversal (relationship constraints)

A typed intent can also carry a `traverse` chain — a list of **relationship hops** that
constrain the queried entity by *what it is connected to*, so questions can span a
connection or cross the two domains (e.g. *"which hazard endangers the Flight Controls
system?"*, *"which aircraft is the Fuel Monitoring Release R1 installed on?"*). The model
still picks **one** entity to return; each hop adds a relationship type, the connected entity
at the far end, a direction (`out` follows `entity-[:REL]->hop`, `in` follows
`entity<-[:REL]-hop`) and optional filters on the hop entity. Only the chosen entity's
fields are returned, so to report a property of a connected node you make *that* node the
queried entity.

The builder turns the chain into **nested `EXISTS` path filters** on the anchor — never a
widened projection:

```cypher
MATCH (n:`Hazard`)
WHERE (n.classification IS NULL OR n.classification IN $__authz_classifications)
  AND EXISTS { MATCH (n)-[:`ENDANGERS`]->(t0:`System`)
               WHERE (t0.classification IS NULL OR t0.classification IN $__authz_classifications)
                 AND t0.`name` = $p0 }
RETURN n.`identifier` AS `hazardIdentifier`, n.`criticality` AS `hazardCriticality`
```

Each hop is authorized independently, so traversal opens no new leak: the relationship type
and its endpoint labels are validated against a **relationship catalog** in the access policy
(`relationshipCatalog` — relationship type → legal `(from, to)` entity-label pairs), the hop's
target entity must be in the principal's granted entities (the same **entity gate**), each hop
filter field must pass the **field gate**, and the row-level **clearance filter** is mirrored
onto every hop node so an anchor can never be discovered through a node above the caller's
clearance. Relationships carry no separate grant — they are gated by the entities they connect,
so a principal that cannot query *either* end can neither traverse nor even see the edge. Only
relationships whose both ends are visible to the identity are described in the prompt surface.

### External document storage (Area 4)

Large document **bodies** are kept **out of the graph** so it scales as a metadata index
rather than a blob store. Each `Document` node holds only metadata — `documentId`, `title`,
`name`, `contentType`, `version`, optional `classification`, an opaque `storageRef`, and a
`checksum` (`sha256:<hex>`). The body itself lives in a pluggable **document store**
(`src/documents/store.py`):

- `LocalFileDocumentStore` (default) reads `data/documents/<storageRef>.md`. The `storageRef`
  is validated to a single path segment, so it can never traverse outside the store root.
- `AzureBlobDocumentStore` is a documented production stub (managed-identity blob access);
  it is not enabled in the PoC. `build_document_store()` always returns the local store today
  (its directory comes from `DOCUMENT_STORE_PATH`); wiring in backend selection there is the
  deliberate production step.

The store is deliberately dumb — it only maps an opaque key to bytes and performs **no**
authorization. The security boundary is the **access service** (`src/documents/access.py`),
reached only through the `fetch_document_content` agent tool, which on every fetch:

1. **Resolves** the model's free-text reference to exactly one document
   (`match_document`: exact `documentId` → exact `title`/`name` → unique substring). An
   ambiguous or unknown reference yields a generic refusal that does not disclose what exists.
2. **Authorizes** it with the *same two dimensions as a graph query* (`authorize_document`):
   the principal must be granted the `Document` entity **and** the `document` sensitivity
   category, and the document's `classification` must be within the principal's clearance.
   `public` has neither and is refused at the entity gate; `maintenance_engineer` and
   `restricted_ops` may read document content.
3. **Verifies integrity** — the fetched bytes are re-hashed and compared to the graph's
   recorded `checksum`; a mismatch raises and the content is withheld.
4. **Sanitises + excerpts** — the body is untrusted text (possible prompt injection), so it
   is normalised, control-stripped, truncated to `DOCUMENT_EXCERPT_CHAR_CAP`, and handed to
   the answer model wrapped in `<<<DOCUMENT CONTENT>>>` markers framed as reference **data**,
   never instructions.

The opaque `storageRef`/blob URI is **never** placed in a tool result, the metadata event, or
the audit trail, so it cannot leak to the LLM. Document fetches are recorded on the same
retrieval sink as graph queries (surfacing a `DOCUMENT FETCH: …` descriptor with provenance —
id/title/version/char-count, **no** body or `storageRef` — in the debug panel) **and** on a
dedicated audit logger, `kg.audit.document`, separate from the `kg.audit` graph trail, with an
explicit outcome per access (`released`/`denied`/`integrity_error`/`store_error`).

### Configuration

Set the Azure OpenAI variables in `backend/.env` (see `.env.example`). They are
optional — `/query` works without them, but `/ask` returns HTTP `503` until they
are present.

| Variable | Example | Description |
| --- | --- | --- |
| `AZURE_OPENAI_ENDPOINT` | `https://<res>.openai.azure.com/openai/v1` | Endpoint. Use the `/openai/v1` form for Azure AI Foundry deployments; a bare resource URL for classic Azure OpenAI. |
| `AZURE_OPENAI_API_KEY` | `<key>` | API key. |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-5.4` | Deployment (model) name. |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` | API version (used only for classic, non-`/openai/v1` endpoints). |
| `DOCUMENT_STORE_PATH` | `../data/documents` | Directory the `LocalFileDocumentStore` reads document bodies from (relative paths resolve against the backend working directory). |
| `DOCUMENT_EXCERPT_CHAR_CAP` | `8000` | Maximum characters of a document body handed to the answer model; longer bodies are truncated. |

### `POST /ask`

The question pipeline streams the answer as the LLM generates it. The request body is:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `question` | string | yes | A natural-language question about the graph. |
| `user` | string | no | Id of the identity asking (see [`POST /users`](#get-users)). Resolved server-side into a principal against the access policy; an unknown or omitted id falls back to the least-privilege default identity (default-deny). |
| `as_of` | string | no | An ISO `YYYY-MM-DD` date. When present, the answer reflects the graph **as it existed on that date**: versioned entities use the version valid then, and event-dated entities (Flights) exclude events that hadn't occurred yet (see [Versioning](#versioning-temporal-data-and-ontology)). Omitted means "current". |

The response is `application/x-ndjson` — a stream of newline-delimited JSON events,
one object per line:

| Event | Shape | When |
| --- | --- | --- |
| `progress` | `{ "type": "progress", "phase": "planning" \| "cypher" \| "querying" \| "document" \| "answering" }` | Repeated, as the pipeline advances through its stages — so the client can show which step is in flight. `cypher`/`querying` accompany a graph query; `document` replaces them when the agent fetches a document body instead. Skipped for off-topic questions. |
| `metadata` | `{ "type": "metadata", "cypher_used": [...], "records": [...] }` | Once, after retrieval, before any tokens. For a document fetch `cypher_used` carries a `DOCUMENT FETCH: …` provenance descriptor (never the `storageRef`) and `records` carries the document's provenance metadata. |
| `token` | `{ "type": "token", "text": "..." }` | Repeated, as answer tokens arrive. |
| `error` | `{ "type": "error", "message": "..." }` | Only on failure (in-band, since headers are already sent). |
| `stats` | `{ "type": "stats", "model": ..., "principal": {...}, "llm_calls": N, "tokens": {...}, "calls": [...], "durations_ms": {...}, "cypher_count": N, "record_count": N, "audit": {...}, "versioning": {...} }` | Once, just before `done` — debug/telemetry for the request. |
| `done` | `{ "type": "done" }` | Always last. |

The `stats` event reports debug telemetry for the request: the model, the acting
`principal` (the resolved identity — `id`, `displayName`, `role`, `clearance`,
`clearanceRank` and the `policyVersion` it was resolved under — so every answer is
attributable to who asked it and under which policy), the number of
LLM calls (agent planning + answer-generation = 2 for an on-topic question — there is no
cypher-generation LLM call because the query is built deterministically), aggregated token
usage with a per-call breakdown, and timings. Each entry in `calls` carries its own
`duration_ms`, and `durations_ms` reports `retrieval`, `graph_query` (the tool's
build-and-run time), `generation` and `total`. Token and duration fields per call let the
UI show how long each LLM call and the graph query took. The agent's planning and answer
tokens are captured per-turn by the `_MafTurnRecorder` middleware (MAF's response otherwise
aggregates them). Token fields are `null` when usage is not reported.

The `stats` event also carries an `audit` object — the per-request record written to
the audit trail (see [Query safety and audit](#query-safety-and-audit)): `outcome`
(`answered` / `refused_off_topic` / `error`), `timestamp`, the principal fields,
`policyVersion`, `schemaFingerprint`, the `cypher` run, `recordCount`, `llmCalls`,
any `denied` constructs (authorization or query-safety denials), and `durationMs`.

The `stats` event also carries a `versioning` object describing the temporal snapshot the
answer was grounded in (see [Versioning](#versioning-temporal-data-and-ontology)): `mode`
(`current` / `as-of`), the `as_of` date (or `null`), `temporal_filter_applied` (whether a
version selection **or** an event-date cutoff was actually injected) and the active
`ontology_version`.

The `progress` events let the client surface the pipeline stage currently in flight.
The retrieval steps run inside the required tool and emit no answer tokens, so without these
events the UI would stall on one label for seconds. Each stage calls a per-request progress
callback at its boundary (`planning` up front, then either `cypher` for the deterministic
build and `querying` for a graph query, or `document` for a document-body fetch, and finally
`answering`); `ask` merges those onto the response stream alongside the answer tokens (the
tool runs in a worker thread, so its progress is marshalled back onto the event loop). The
phases mirror the steps in the chat UI's debug panel.

Because retrieval runs first, the client receives the Cypher and rows up front and can
render the answer progressively. The retrieval step runs in a worker thread while
tokens are produced via the async OpenAI client; `asyncio.CancelledError` (client
disconnect) closes the upstream stream cleanly. The same 503 applies if Azure OpenAI
is not configured (raised before streaming begins).

**Off-topic questions** are rejected by the relevance guardrail before any retrieval or
LLM call: the stream is still well-formed (`metadata` with empty `cypher_used`/`records`,
a `token` carrying a fixed refusal message, a zero-usage `stats` event with
`llm_calls: 0`, then `done`), so clients need no special handling.

```bash
curl -N -X POST http://localhost:8080/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "How many flying hours has the engine had since 2026-05-25?", "user": "maintenance_engineer"}'
```

```
{"type": "progress", "phase": "planning"}
{"type": "progress", "phase": "cypher"}
{"type": "progress", "phase": "querying"}
{"type": "progress", "phase": "answering"}
{"type": "metadata", "cypher_used": ["MATCH (ac:Aircraft)-..."], "records": [{"engine": "Lycoming IO-360", "flights": 4, "hours": 2.2}]}
{"type": "token", "text": "Since "}
{"type": "token", "text": "2026-05-25"}
...
{"type": "stats", "model": "gpt-5.4", "principal": {"id": "maintenance_engineer", "displayName": "Maintenance Engineer", "role": "maintenance", "clearance": "unclassified", "clearanceRank": 0, "policyVersion": "2026-06-10.5"}, "llm_calls": 2, "tokens": {"prompt": 1494, "completion": 78, "total": 1572}, "calls": [{"stage": "agent_planning", "prompt": 940, "completion": 39, "total": 979, "duration_ms": 720.3}, {"stage": "answer_generation", "prompt": 554, "completion": 39, "total": 593, "duration_ms": 1269.9}], "durations_ms": {"retrieval": 11.4, "graph_query": 11.4, "generation": 1269.9, "total": 2001.6}, "cypher_count": 1, "record_count": 6, "audit": {"timestamp": "2026-06-09T12:00:00.000+00:00", "outcome": "answered", "user": "maintenance_engineer", "role": "maintenance", "clearance": "unclassified", "policyVersion": "2026-06-10.5", "schemaFingerprint": "a1b2c3d4e5f6", "question": "How many flying hours...", "cypher": ["MATCH (n:`Flight`) RETURN sum(n.`flightTime_hours`) AS result"], "recordCount": 6, "llmCalls": 2, "denied": [], "durationMs": 2001.6}}
{"type": "done"}
```

For a **document** question the same endpoint instead drives the `fetch_document_content`
tool — note the `document` progress phase and the `DOCUMENT FETCH` provenance descriptor in
place of a Cypher query (the opaque `storageRef` is never surfaced):

```bash
curl -N -X POST http://localhost:8080/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "What does the POH say about the never-exceed speed?", "user": "maintenance_engineer"}'
```

```
{"type": "progress", "phase": "planning"}
{"type": "progress", "phase": "document"}
{"type": "progress", "phase": "answering"}
{"type": "metadata", "cypher_used": ["DOCUMENT FETCH: DOC-0001 \"Pilot's Operating Handbook — C172S\" v3 (1010 chars, checksum verified)"], "records": [{"documentId": "DOC-0001", "title": "Pilot's Operating Handbook — C172S", "version": 3, "contentType": "text/markdown", "charCount": 1010, "truncated": false}]}
{"type": "token", "text": "The POH "}
{"type": "token", "text": "states the never-exceed speed (Vne) is 163 KIAS."}
...
{"type": "stats", "model": "gpt-5.4", "principal": {"id": "maintenance_engineer", ...}, "llm_calls": 2, "cypher_count": 1, "record_count": 1, "audit": {"outcome": "answered", "cypher": ["DOCUMENT FETCH: DOC-0001 \"Pilot's Operating Handbook — C172S\" v3 (1010 chars, checksum verified)"], ...}, "versioning": {...}}
{"type": "done"}
```

A `public` identity asking the same question is **refused** at the entity gate (it holds
neither the `Document` entity nor the `document` category), and the refusal is recorded on
the `kg.audit.document` trail.

### `GET /users`

Lists the selectable identities, so the chat UI can offer an identity selector without
hard-coding the list (it is policy-driven). Returns the access policy version and the
identities defined in it:

```bash
curl http://localhost:8080/users
```

```json
{
  "version": "2026-06-18.1",
  "users": [
    {"id": "public", "displayName": "Public (least privilege)", "role": "public", "clearance": "unclassified", "description": "..."},
    {"id": "maintenance_engineer", "displayName": "Maintenance Engineer", "role": "maintenance", "clearance": "unclassified", "description": "..."},
    {"id": "restricted_ops", "displayName": "Restricted Operations", "role": "operations", "clearance": "secret", "description": "..."},
    {"id": "software_engineer", "displayName": "Software Engineer", "role": "engineering", "clearance": "unclassified", "description": "..."}
  ]
}
```

## Authorization model

> **PoC scope.** This is an *application-level* authorization layer built to demonstrate
> the idea — not enterprise ABAC. It runs against stock **Neo4j Community Edition**, which
> has no in-database access control, so the backend is the *only* enforcement boundary. See
> [Neo4j Community Edition: trade-offs](#neo4j-community-edition-trade-offs-and-enterprise-alternatives)
> for what a production deployment on Enterprise could push into the database itself.

The backend is the **authorization trust boundary**: the LLM is treated as untrusted, and
the question is *default-deny* — an identity sees nothing it has not been explicitly
granted. Authorization is **two-dimensional**, and both dimensions are checked before any
data is read:

1. **Capability grants (role-based, coarse).** What *kinds* of thing an identity may query:
   which **entities** (node labels), which **sensitivity categories** of field, and whether
   it may run **aggregates**. These come from the identity's role in the policy.
2. **Clearance (row-level, fine).** A clearance level (`unclassified` → `official` →
   `secret`) compared against each node's own in-graph `classification` property, so whole
   *rows* (e.g. a classified military flight) can be hidden even within an entity the
   identity may otherwise query. A category can optionally be **clearance-gated** so that
   classified rows stay visible but the gated fields are protected field-by-field instead of
   the whole row being hidden (see [Clearance-gated categories](#clearance-gated-categories)).

### The policy is data, not code

The access policy lives in [`policy/access-policy.json`](policy/access-policy.json)
(override with `ACCESS_POLICY_PATH`). It is **versioned separately** from the graph data
and the ontology, so *who may see what* can change without re-importing the graph, and
every answer records the `policyVersion` it was resolved under. It is loaded and validated
at startup; an invalid or missing policy **fails the service closed**. Its five parts:

| Key | What it defines |
|---|---|
| `clearanceLevels` | The ordered clearance ladder (`unclassified` < `official` < `secret`). |
| `sensitivityCategories` | The field-sensitivity labels (`basic`, `duration`, `route`, `maintenance`, `performance`) plus `document`, the capability that gates reading an externalised document **body**, and `engineering`, which gates the SDLC domain. |
| `catalog` | The **curated, queryable surface**: every entity, each field on it, and the one sensitivity category that field belongs to. *A field absent from the catalog is queryable by no one* (default-deny). |
| `relationshipCatalog` | The **traversable surface**: each relationship type and the legal `(from, to)` entity-label pairs it may connect. A hop is only allowed if it matches an entry here *and* the identity is granted both endpoint entities — relationships have no separate grant (see [Cross-domain traversal](#cross-domain-traversal-relationship-constraints)). |
| `identities` | The selectable identities, each with a `role`, a `clearance`, the `categories` and `entities` it is granted, whether any of those categories are `clearanceGatedCategories` (see below), and whether it `allowAggregates`. |

The demo identities make the two dimensions concrete:

| Identity | Clearance | Entities | Categories | Aggregates | Cannot see |
|---|---|---|---|---|---|
| `public` | unclassified | Aircraft, Specification, Aerodrome | `basic` | ❌ | maintenance, durations, routes, classified flights, document content |
| `maintenance_engineer` | unclassified | + System, Component, Document, Flight | + duration, maintenance, performance, **document**, **route (clearance-gated)** | ✅ | route details **on classified flights** |
| `restricted_ops` | secret | (same entities) | + route (full) (incl. **document**) | ✅ | — (sees everything, incl. classified flights) |
| `software_engineer` | unclassified | Aircraft, System, Component, PistonEngine, Specification, Aerodrome **+ the SDLC entities** (Requirements, Implementation, Verification, Assurance, Safety, Configuration, Work Management, Actor) | `basic`, **`engineering`** | ✅ | flights, routes, durations, maintenance and document content |

So a maintenance engineer can ask for *total flight time* (a `duration` field, aggregated)
and *can* see where the aircraft flew — but only on **unclassified** flights: `route` is a
**clearance-gated** category for that identity (see below), so route fields are nulled on the
classified (military) flights it isn't cleared for, while a flying-hours total still counts
those flights. It also holds the `document` category, so it may read document **bodies** via
`fetch_document_content` (the POH, manuals, ADs); `public` holds neither the `Document` entity
nor the `document` category, so document content is refused at the entity gate.
`restricted_ops` holds `route` outright and sees route details on every flight. The
`software_engineer` persona is the entry point to the **engineering domain**: it holds the
`engineering` category and the SDLC entities, so it can ask about requirements, work items,
pull requests, hazards, test results, release baselines and the like, plus `basic`
operational facts about the aircraft and its systems — but it has no access to the flight,
route, maintenance or document data the operational identities hold.

#### Clearance-gated categories

A granted category is normally all-or-nothing per row: the row-level clearance filter hides a
whole classified row, so its fields never appear (not even in a `count`). An identity can
instead mark a category as **clearance-gated** (`clearanceGatedCategories`), a middle ground
between the two dimensions: classified rows stay **visible**, but that category's fields are
protected **field-by-field** rather than the whole row being hidden. For those rows the gated
fields are nulled in the projection (a `CASE WHEN` on the row's clearance), gated-field filters
cannot match them, and gated-field aggregates exclude them — while **non-gated** fields, plain
`count`s, and aggregates over **non-gated** fields still include them. This is what lets
`maintenance_engineer` total flying hours across *all* flights (including military ones) yet
never learn where a classified flight went. Clearance-gating is a deliberate, auditable
relaxation — it reopens an existence/inference channel for those rows — so it is **off by
default** and granted per-identity only where need-to-know requires it.

### How an identity becomes a `Principal`

A request names an identity (`user` on `/ask`); the backend resolves it **server-side** —
never trusting the client beyond the choice of id — via `PolicyStore.resolve_principal`
into a `Principal` carrying the role, clearance, granted categories/entities, the set of
classifications the clearance permits, and the policy version. An unknown or omitted `user`
resolves to the policy's least-privilege `defaultIdentity` (`public`), not to broad access.

> **Not authentication.** This is identity *selection* for the PoC (a UI dropdown). In a
> real deployment the id would arrive in a verified token (e.g. an OIDC claim), not from the
> client. The enforcement mechanics below would be unchanged.

### Where it is enforced

Enforcement happens entirely in the backend, in two places, both outside the LLM:

- **Per-request surface scoping.** The agent is rebuilt per request so its system
  instructions and tool surface expose **only** the entities and fields the principal may
  see (`PolicyStore.describe_surface`). Unauthorised field *names* never reach the model, so
  it cannot even *ask* for them. The principal is bound to the tool by closure — it is never
  a tool argument, so the model can neither see nor spoof it.
- **The structured-intent query builder** ([`src/authz/query_builder.py`](src/authz/query_builder.py)).
  Every typed intent passes, in order: an **entity gate** → a **field gate** (every
  projected *and* filtered field must be visible) → an **aggregate gate** → an
  always-injected **row-level clearance filter**
  (`n.classification IS NULL OR n.classification IN $__authz_classifications`). Because the
  filter is injected *before* aggregation, classified rows never participate in a query —
  not even in a `count` or existence check. (For a **clearance-gated** category the whole-row
  filter is replaced by per-field `CASE WHEN` redaction plus clearance guards on gated-field
  filters and aggregates, so classified rows stay visible with only the gated fields nulled.)
  A `traverse` chain adds a **relationship gate** per hop — the relationship type and its
  endpoint labels are validated against the policy's `relationshipCatalog`, the hop's target
  entity must be granted (the entity gate again), hop filter fields pass the field gate, and
  the clearance filter is mirrored onto every hop node — all emitted as nested `EXISTS`
  constraints that never widen the projection (see
  [Cross-domain traversal](#cross-domain-traversal-relationship-constraints)).
  Values are always parameterised; labels and field names come only from the controlled
  catalogue and are identifier-validated. A denied intent records an audit denial and returns
  a refusal string (never a 500). Returned rows are then **redacted** to the projected fields
  as a final defence-in-depth net.

The full request-time mechanics — guardrail, the two LLM turns, the gate order and the
redaction net — are documented step-by-step under
[Natural-language questions → How it works](#how-it-works). The code lives in
[`src/authz/`](src/authz/): `models.py` (the policy/`Principal` models), `store.py`
(`PolicyStore`) and `query_builder.py` (the enforcement layer).

## Versioning (temporal data and ontology)

Three things in this system version on independent clocks: the **graph data**, the **access
policy** (who may see what — above), and the **ontology** (what the schema means). And the
*data* itself carries two different temporal concepts that are easy to conflate but must be
kept distinct:

- **Valid-time versioning** — a *logical entity is revised* over time (e.g. a performance
  `Specification` re-measured every few years). Each revision is a version; an as-of query
  selects the version valid then.
- **Event time** — an *event happened* at a point in time (e.g. a `Flight`). An event is not a
  "version" of anything; it simply did or did not exist yet. An as-of query includes only
  events that had already occurred.

Both are driven by the *same* as-of date and unify under one idea: **the knowledge graph as it
existed on that date**. Both filters are injected deterministically by the backend (never the
LLM), and both are kept deliberately **narrow** — versioning the whole graph would force the
model to reason about validity windows it cannot see, mixing current and historical facts.

### How versioned data is modelled

Each version of a logical thing is its **own node**. They share a stable `logicalId` and
differ by `version`, with `validFrom`/`validTo` describing the window each was valid and a
boolean `current` flag marking the live one. Consecutive versions are chained by a
`PREVIOUS_VERSION` relationship (newer → older). So the database holds **all** versions —
nothing is overwritten — and "current" is just a flag, not a deletion of the past. In the
shipped data the `perf` (performance) specification has three versions with deliberately
divergent figures (max cruise speed 108 → 122 → 140 kt across the eras); the other
specifications are single-version. Every `Specification` node carries the version properties,
so the temporal filter below never encounters a missing field.

The full history lives in the shared `data/aircraft-knowledge-graph.json` export (unlike the
classification overlay, versioning is graph structure, not a security secret), so the Vue
graph renderer can show current vs as-of slices and per-entity history too.

**Structural versioning (renderer illustration).** To make versioning *visible* in the graph
(not just a changed number behind a click), the shipped data also models an **avionics
retrofit**: an `avpkg` ("avionics package") logical node with three versions — *Analog Panel*
→ *GPS-Equipped Panel* → *G1000 Glass Cockpit* — each wired to a different set of versioned
instrument nodes, and the existing glass-cockpit components marked as the current era. As you
move the renderer's as-of date the **topology visibly changes** (different nodes, labels and
counts appear). This subgraph is a **renderer-side illustration only**: its labels
(`AvionicsPackage`, `Instrument`, and the retrofitted `Component`s) are *not* in
`VERSIONED_ENTITIES`, so the backend query builder never injects a temporal filter for them —
the backend's temporal querying is scoped to `Specification` (valid-time) and `Flight`
(event-time).

### Two query modes — current and as-of

The temporal filter is **never the LLM's job** — exactly like the clearance filter, it is
injected deterministically by the builder, and only for versioned entities
(`VERSIONED_ENTITIES` in [`src/authz/query_builder.py`](src/authz/query_builder.py)):

- **Current** (default): `AND n.current = true` — only the live version participates.
- **As-of** a date (`as_of` on `POST /ask`, an ISO `YYYY-MM-DD`):
  `AND n.validFrom <= $__asOf AND (n.validTo IS NULL OR $__asOf < n.validTo)` — only the
  version valid on that date participates.

This is the same selective approach authz uses: the predicate is added **only** for versioned
labels, so unversioned entities are queried exactly as before and never hit a null-property
trap (a blind `n.current = true` against a node with no `current` property would silently drop
every row). The acting identity's *current* policy is always evaluated against the selected
data snapshot — we time-travel the data, not the permissions. The mode, the `as_of` date and
whether a temporal filter was actually applied are reported in the debug `stats.versioning`
block so the panel reflects exactly what ran.

### Event-dated entities — the as-of cutoff for events

`Flight` is **event-dated**, not versioned: each flight is an immutable event with an ISO
`date`. Entities in `EVENT_DATED_ENTITIES` (in
[`src/authz/query_builder.py`](src/authz/query_builder.py)) get a different predicate, and
only under an as-of query:

- **Current** (default): no cutoff — every event is in scope.
- **As-of** a date: `AND n.date <= $__asOf` — only events that had already occurred by that
  date participate.

So asking "how many flights?" as-of an early date returns only the flights flown by then, and
in the renderer flights (and their flight phases) appear progressively as you advance the
date. This is the event-time half of "the graph as it existed on that date", kept separate
from versioning because an event genuinely is not a version — it has no `current` flag and no
validity window, just an occurrence date.

### Ingest validation

The import script ([`scripts/import_graph.py`](scripts/import_graph.py)) checks, after
loading, that **no `logicalId` has more than one `current=true` version** and that version
numbers are unique within a `logicalId`. An ambiguous "current" would make the `current`
filter silently wrong, and idempotent re-imports can otherwise leave several current rows
(e.g. a botched edit). **Zero** current versions is allowed — that is a fully *retired*
logical entity (the analog/GPS avionics packages and instruments, replaced by the glass
retrofit, have no live version), which is a legitimate state in a deprecate-don't-delete model.

### Ontology versioning

The active ontology is described by [`policy/ontology.json`](policy/ontology.json) and loaded
by [`src/common/ontology.py`](src/common/ontology.py): a semantic `version` plus a list of
**deprecated-but-retained** terms (deprecate-don't-delete — an old term stays interpretable
after the data moves to its replacement). The active version (and any deprecations) is added
to the answer model's retrieval context and recorded in `stats.versioning.ontology_version`,
so every answer is attributable to the ontology it was grounded in.

### Community Edition note

Enforcing the "at most one current version per `logicalId`" invariant is done in the **import
script** because Community Edition has no **node-key / property-existence constraints** to
enforce it in the database (only uniqueness constraints exist). There is likewise no
temporal/bitemporal storage type, so as-of is an application-level string-date comparison over
ISO dates (lexicographic comparison is correct for `YYYY-MM-DD`). On Enterprise you could back
the invariant with a constraint and model validity with native `date`/`datetime` types. See
[Neo4j Community Edition trade-offs](#neo4j-community-edition-trade-offs-and-enterprise-alternatives).

## Query safety and audit

The backend builds every query deterministically from a validated intent, and bounds and
records each one as part of the same trust boundary.

- **Query safety** — [`src/common/query_safety.py`](src/common/query_safety.py).
  `assert_safe_cypher` is defence-in-depth on top of the query builder only ever emitting
  read-only `MATCH … RETURN`: it deterministically rejects empty input, multiple statements,
  write clauses, procedure calls (`CALL`), `LOAD CSV`, database switching (`USE`) and
  schema/admin namespaces (`db.`/`dbms.`/`apoc.`/`gds.`/`cdc.`/`sys.`). String and backtick
  literals are stripped first, so a value that merely *spells* a keyword cannot trip it. The
  agent's own Neo4j driver is wrapped so the check runs before any query executes; a denial
  degrades gracefully (no rows) and is recorded for audit. Each query is also bounded by a
  per-statement **timeout** (`QUERY_TIMEOUT_SECONDS`, default `10`) and a post-fetch **row
  cap** (`QUERY_ROW_CAP`, default `1000`) that limits how much data reaches the answer step.
  The same function is reused by the structured-intent query builder, so query safety has a
  single home.
- **Audit trail** — [`src/common/audit.py`](src/common/audit.py). Every answered, refused or
  failed request produces one `AuditRecord` written to a dedicated `kg.audit` logger (so it
  can be routed/retained independently, and is exported to Application Insights when
  telemetry is configured) and embedded as `stats.audit` for the debug panel. It records who
  asked (principal + `policyVersion`), the `schemaFingerprint` shown to the LLM, the question,
  the Cypher run, the record count, the LLM-call count, any query-safety `denied` constructs,
  the `outcome` and the duration — making every answer attributable to an identity and a
  policy version.
- **Document-access audit** — externalised document-body fetches are additionally recorded on
  a dedicated `kg.audit.document` logger (separate from the `kg.audit` graph trail), one line
  per access with an explicit `outcome` (`released`/`denied`/`integrity_error`/`store_error`),
  the principal, the requested reference and the resolved `documentId`/`version`/char-count —
  never the opaque `storageRef`. Document fetches also appear in the debug panel as a
  `DOCUMENT FETCH: …` provenance descriptor. See
  [External document storage](#external-document-storage-area-4).

| Variable | Default | Purpose |
|---|---|---|
| `ACCESS_POLICY_PATH` | `policy/access-policy.json` | Location of the versioned access policy. |
| `QUERY_TIMEOUT_SECONDS` | `10` | Per-statement Neo4j timeout. |
| `QUERY_ROW_CAP` | `1000` | Max rows passed from retrieval to the answer step. |

## Neo4j Community Edition: trade-offs and Enterprise alternatives

This PoC runs against **stock Neo4j Community Edition** (`neo4j:latest`, started by
[`scripts/start-database.sh`](../scripts/start-database.sh)). Community has a single user
and **no in-database access control of any kind**. Several design decisions here exist
specifically to work around that — and would be implemented differently, often more
robustly, on **Neo4j Enterprise Edition**. They are called out honestly so a reviewer can
see where the PoC simulates something the database could enforce natively.

| Decision in this PoC | Why (Community limitation) | Enterprise alternative |
|---|---|---|
| Authorization lives entirely in the **application** (policy store + structured-intent query builder + injected clearance filter + redaction). | Community has **no RBAC** and **no label / relationship / property-level security** — you cannot express "this role can't read this property" in the database. | Native **fine-grained access control**: `GRANT`/`DENY TRAVERSE`/`READ`/`MATCH` on labels, relationship types and **properties** (sub-graph + property-level security). Much of the clearance/category gating could be enforced *inside the database* as a second hard boundary, so an application bug alone could not leak data. |
| Everything is one database; identity is selected, not authenticated. | Community supports a **single user database** and only basic auth. | Multiple databases, plus native users/roles — identity and tenancy can be modelled in the DBMS. |
| A *future* federation story would need application-level stitching of separate graphs. | Community has **no composite databases / Fabric**. | **Composite databases (Fabric)** federate and shard queries across multiple databases natively. |
| `classification` is a plain node property, and a **missing** `classification` is treated as *unclassified / visible to all* (`IS NULL OR …`). Integrity relies on the import data being correct. | Community supports only **uniqueness** constraints — not **existence** or **node-key** constraints — so you cannot force every node to carry a `classification`. | **Property existence constraints** (`REQUIRE n.classification IS NOT NULL`) guarantee the property is always present, so the policy could safely **default-deny** an unclassified node instead of defaulting to visible. |

The honest framing: the application-layer approach is portable and keeps the policy
external and inspectable, but on Community it is the **only** boundary — a bug in the query
builder is a data leak. On Enterprise you would keep the structured-intent layer (for the
typed surface, auditability and answer grounding) *and* back it with in-database privileges
so the database independently refuses unauthorised reads.

## Evaluation

`uv run poe evaluate` runs an **offline, deterministic** evaluation of the `/ask`
pipeline against a pre-baked ground-truth file — **no LLM-as-judge**. It drives the
in-process `KnowledgeGraphAgent` (the same code path the API uses), so it needs the
same Neo4j and Azure OpenAI settings in `backend/.env`, and the graph must already be
imported.

The point is a **repeatable, judge-free signal**: every metric is computed by comparing
the agent's tool selection, structured intent and retrieved rows (or selected document
content) against hand-written gold data with plain set arithmetic and string matching — the
streamed natural-language answer is recorded for human review but **never scored**. A run is
cheap, deterministic given the data, and diffable across code changes (inspect any run in the
[dashboard](#dashboard)). By
default it drives the pipeline as the **most-privileged identity** so retrieval quality is
measured without authorization capping the results; pass `--user <id>` to evaluate as a
specific identity instead. Individual ground-truth cases can override that identity (and
the temporal snapshot) per question, so the harness also covers **authorization** and
**temporal** behaviour, not just retrieval quality (see [Ground truth](#ground-truth)).
Authorization *enforcement* itself is additionally verified by the
adversarial unit tests (`tests/test_query_builder.py`, `tests/test_knowledge_graph_agent.py`).

> **PoC caveat.** The ground-truth set is small and hand-curated, scoring is deterministic
> (it never grades the natural-language answer), and runs cost real Azure OpenAI tokens. It
> is a development feedback tool, not a statistically rigorous benchmark.

```bash
uv run poe evaluate                                   # eval/ground_truth.json -> eval/results/eval-<timestamp>.json
uv run python scripts/evaluate.py --question-id flight-count --output -   # one question, report to stdout
```

### Ground truth

[`eval/ground_truth.json`](eval/ground_truth.json) holds a list of questions. Each one
falls into one of three **modes**, inferred from its `expected_tools`, so a single file can
exercise retrieval quality, document answers, authorization and temporal snapshots:

- **retrieval** — `expected_tools` is `["query_knowledge_graph"]`; scored on the structured
  query intent the model emitted (`expected_intent` — see [Intent scoring](#intent-scoring))
  and on the **tool output** against a single, hand-written known answer
  (`expected_output_rows` / `expected_output_fields` — see [Output scoring](#output-scoring)).
  There is no second live "gold query": the expected values are fixed data, derived once from
  the seeded graph. The derivation queries are kept in
  [`eval/ground_truth_provenance.json`](eval/ground_truth_provenance.json) for traceability
  only — they are documentation and are **never executed** by the harness.
- **document** — `expected_tools` is `["fetch_document_content"]` (e.g. a question whose
  content lives outside the graph); scored on selecting the document tool and performing a
  successful, non-empty fetch, plus — when declared — the **output** of that fetch: that the
  expected document was selected (`expected_document_id`) and its content carries the expected
  facts (`expected_output_values`). The natural-language *wording* is never compared (that
  would need an LLM judge); the document the tool returned is.
- **refusal** — `expect_refusal: true`; the *correct* outcome is that the identity is told
  the information is not available. Passes when the pipeline records an authorization
  **denial** in its `stats` audit trail **and** no `forbidden_answer_values` reach the
  retrieved data.

Common optional fields: `forbidden_answer_values` (values that must **never**
appear in the retrieved rows — an authorization-leak guard), `expected_tools` (the tool
name(s) a correct answer should invoke — `["query_knowledge_graph"]` or
`["fetch_document_content"]`; required for every non-refusal case), `expected_intent` (the
structured query intent a correct answer should emit — see [Intent scoring](#intent-scoring)),
`expected_output_rows` / `expected_output_fields` / `expected_output_values` /
`expected_document_id` (the tool's **output** — see [Output scoring](#output-scoring)),
`exact_output` (opt into the over-fetch/precision guard — see below), plus `user` (drive the
case as a specific identity) and `as_of` (an `YYYY-MM-DD` temporal snapshot — only flights
that had occurred by then participate):

```json
{
  "id": "powerplant-components",
  "question": "How many components does the aircraft have?",
  "expected_intent": { "entity": "Component", "aggregate": { "func": "count" } },
  "expected_output_rows": [ { "componentResult": 112 } ],
  "exact_output": true,
  "expected_tools": ["query_knowledge_graph"]
}
```

#### Intent scoring

Tool selection alone is necessary but not sufficient: the agent can pick the right tool yet
choose the **wrong thing to fetch** (e.g. querying the `Specification` entity instead of
`Aircraft`, or aggregating when the question asks for a row). Because the backend builds
Cypher **deterministically** from the model's typed `QueryIntent` (entity + fields + filters
+ optional aggregate — there is no text-to-Cypher), that intent *is* the model's retrieval
decision and is machine-checkable.

A retrieval case may declare `expected_intent`, which is matched against the intent the model
actually emitted. The match is **value-based, order-independent, and a partial contract** —
only the keys you declare are checked, so you can pin just the part that matters (the
`entity`, an `aggregate`, a specific `filter`) without over-fitting to incidental field
selection. `fields`/`filters` are compared as sets; scalar values tolerate float formatting.
The shipped ground truth declares the **complete** intent for each retrieval case — `entity`
plus the exact `fields`, `filters` and any `aggregate` the correct query uses — so the
expected intent mirrors the one the model emits and the [dashboard](#dashboard) can show them
side by side; the partial-contract matcher still lets you pin only a subset when authoring new
cases. When declared, a wrong intent **fails** the case even if the rows scored. The run summary
reports `intent_selection_accuracy` over the cases that declared an `expected_intent`.

#### Output scoring

Tool selection and intent scoring check *what the agent decided to do*; output scoring checks
*what the chosen tool actually returned* against fixed ground-truth values — the **value from
the Cypher query** or the **content of the selected document**, depending on the tool.

A query returns **columnar rows**, so its output is scored **column-aware** against the
agent's *deterministic* output column names. Those names are not chosen by the LLM: the
backend builds Cypher from the typed `QueryIntent`, and the builder aliases every projected
field as an **entity-qualified camelCase** name — `<entityCamel><FieldPascal>` (e.g.
`n.\`ratedHorsepower\` AS \`pistonEngineRatedHorsepower\``, `aircraftMaxTakeoffWeight_kg`), every
aggregate as `<entityCamel>Result` (e.g. `flightResult`, `componentResult`), and a resolved
aerodrome companion as `<codeAlias>Name` (e.g. `flightDestinationAerodromeName`). Qualifying
the alias with the entity is what lets the output check prove *the right value in the right
field for the right entity* — two entities that share a bare field name (`name`, `model`)
no longer collide. (This alias contract is locked by a unit test in `test_query_builder.py`,
since the eval depends on it.)

- `expected_output_rows` — a list of **partial rows** the retrieved data must contain. Each
  spec must be satisfied by a **single** returned record where **every** named field matches,
  so it proves *the right value in the right field, in the same row* (a flat value check could
  pass on facts split across different rows). Values match by **exact canonical equality**
  (numbers are normalised so `1157` / `1157.0` and float-rounding noise compare equal; `180`
  does **not** match `1180`). Substring matching is opt-in per field via `{ "contains": "…" }`.
  Use the aggregate alias as the key for an aggregate (e.g. `[{ "flightResult": 12 }]`).
- `exact_output` — when `true`, the case **also** fails if the agent returned any record that
  matches **none** of the `expected_output_rows` (an over-fetch / precision guard). Set it
  when the expected rows enumerate the *complete* answer, so a query that returns the right
  row plus extra noise (e.g. reaching into a second entity) is caught. The run summary reports
  `overfetch_count` across the cases that set it.
- `expected_output_fields` — per-column coverage **without** pinning row identity:
  `{ column: value | [values] }` requires each value to appear somewhere in that column across
  the returned records. Use it for open-ended multi-row sets (e.g. a list of aerodrome names).
- `expected_output_values` — **document cases only**: values that must appear in the selected
  document's *content* (a document body has no columns). Matching is case-insensitive substring
  and tolerates numeric surface forms (`1157` / `1,157`). The content is surfaced to the
  harness on the `documents_used` metadata field.
- `expected_document_id` — for a document case, the id of the document a correct answer should
  select (e.g. `"DOC-0001"`), scoring *which* document the tool returned.

When declared, a missing row/field/value, an over-fetched row (`exact_output`), or the wrong
document **fails** the case. This catches answers that ran the right tool but produced the
wrong data — e.g. a query that retrieves the wrong entity, omits the field the question asks
for, returns the right number in the wrong column, or over-fetches; or a document fetch that
pulled the wrong handbook. The run summary reports `output_value_accuracy` and
`document_selection_accuracy` over the cases that declared each.

```json
{
  "id": "engine-model",
  "question": "What is the make and model of the engine, and how much horsepower does it produce?",
  "expected_intent": { "entity": "PistonEngine", "fields": ["manufacturer", "model", "ratedHorsepower"] },
  "expected_output_rows": [
    { "pistonEngineManufacturer": "Lycoming", "pistonEngineModel": "IO-360-L2A", "pistonEngineRatedHorsepower": 180 }
  ],
  "exact_output": true,
  "expected_tools": ["query_knowledge_graph"]
}
```

```json
{
  "id": "poh-never-exceed-speed",
  "question": "What does the POH say about the never-exceed speed?",
  "user": "maintenance_engineer",
  "expected_document_id": "DOC-0001",
  "expected_output_values": ["163"],
  "expected_tools": ["fetch_document_content"]
}
```

```json
{
  "id": "public-denied-poh",
  "question": "What does the POH say about the never-exceed speed?",
  "user": "public",
  "expect_refusal": true
}
```

### Metrics

Scoring is **deterministic** and derives only from signals that don't need a judge: which
tool the agent picked, the structured intent it emitted, whether the query was valid, and the
**raw rows or document content it retrieved** — compared against fixed, hand-written
ground-truth values. The streamed natural-language answer is recorded for human review but
**never scored** — grading its wording would require an LLM-as-judge, which this harness
deliberately avoids. There is **no live gold query**: the expected values are a single,
hand-written known-answer oracle (`expected_output_rows` / `expected_output_fields`), derived
once from the seeded graph and stored in the ground truth; the derivation Cypher lives in
`eval/ground_truth_provenance.json` for traceability and is never run. For each question the
script drives the agent to get its generated query, retrieved rows and answer (from the
`metadata`/`token`/`stats` debug events), then computes:

- **Tool selection**: `tool_selection_accuracy` — over the cases that declared
  `expected_tools`, how often the agent invoked exactly the right tool(s). Picking the wrong
  tool (e.g. running a graph query for a document question) fails the case outright.
- **Intent selection**: `intent_selection_accuracy` — over the cases that declared an
  `expected_intent`, how often the model's emitted intent matched (see
  [Intent scoring](#intent-scoring)).
- **Output value**: `output_value_accuracy` — over the retrieval cases that declared
  `expected_output_rows` / `expected_output_fields`, how often the retrieved rows carried the
  expected values in the expected (entity-qualified) columns, plus `overfetch_count` — how
  many `exact_output` cases returned a row outside the expected set (see
  [Output scoring](#output-scoring)).
- **Output precision / recall / F1**: `output_precision_macro` / `output_recall_macro` /
  `output_f1_macro` (and their `_micro` counterparts) — a set-overlap view of the same
  retrieval cases. For each case the harness counts true positives (expected row/field values
  the agent returned), false negatives (expected values it missed) and false positives (values
  it returned that the oracle does not list), then derives precision/recall/F1. **Macro** is
  the unweighted mean of the per-case scores; **micro** pools the tp/fp/fn across all cases
  before dividing. There is **no live gold query** — the counts come from the hand-written
  oracle, so field-level precision assumes the listed values enumerate the column's complete
  set. These complement (not replace) the boolean `output_value_accuracy` and over-fetch
  guards: a case can pass the boolean check yet score &lt;1.0 F1 if it over- or under-fetches
  values within the expected columns.
- **Document**: `document_fetch_rate` — of the document-mode cases, the fraction that
  selected the document tool and returned a non-empty fetch; and `document_selection_accuracy`
  over the cases that declared `expected_document_id`.
- **Operational**: query **validity** rate, **empty-retrieval** rate, and token/latency
  **cost** taken from the pipeline's own `stats` event.
- **Authorization**: `refusal_correct_rate` — of the `refusal`-mode cases, the fraction the
  pipeline correctly denied; and `forbidden_leak_count` — across *all* cases, how many
  surfaced a `forbidden_answer_values` in the **retrieved data** (authorization is enforced
  on the data, so a leak shows up there; should always be `0`).

Each metric is averaged only over the cases that carry the relevant fields, so mixing
retrieval, document, authorization and tool-selection cases in one file does not distort the
numbers.

The metric functions live alongside the CLI in
[`scripts/evaluate.py`](scripts/evaluate.py) and are unit-tested in
[`tests/test_evaluation.py`](tests/test_evaluation.py).

### Output

A single JSON report is written to `eval/results/` (git-ignored) containing run metadata,
an overall `summary`, and full per-question detail
(the generated query, the **actual retrieved rows**, retrieved row count, all metrics, the
tools used, the unscored answer text and the cost). A compact summary is also logged at the
end of the run.

### Dashboard

[`eval/dashboard.html`](eval/dashboard.html) is a dependency-free HTML dashboard that
loads **every** report in `eval/results/`, showing summary cards (pass rate, output values,
output precision/recall/F1, over-fetch, tool/intent selection, document and authorization
metrics) and expandable per-question detail (pick a run from the dropdown). Each question lays
the **expected** values out against the agent's **actual** output side by side — expected
intent vs the emitted intent, expected rows/fields vs the actual retrieved rows, and (for
document cases) the expected content values (with which were found / missing) vs the **actual
fetched document body** that those values were substring-matched against — with per-question
precision/recall/F1 and tp/fp/fn in the retrieval table, and the unscored answer shown at the
foot of the panel. A static file cannot list a directory over `file://`, so serve the folder:

```bash
cd eval && python -m http.server 8000
# then open http://localhost:8000/dashboard.html
```

It discovers result files by parsing the served directory listing (it also honours an
optional `results/index.json` manifest if one exists).

## Logging and observability

The backend logs through a single application logger (root name `kg`) configured at
startup in `app.py` via `setup_logging()`. Set `LOG_LEVEL` in `backend/.env` to control
verbosity:

| Variable | Default | Description |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). `DEBUG` adds per-step pipeline detail (schema fetch, guardrail decision, structured-intent build, query execution, answer generation). |

Noisy third-party libraries (`neo4j`, `openai`, `httpx`, `opentelemetry`, `azure.*`)
are pinned to `WARNING` so the application's own logs stay readable, even at `DEBUG`.

Distributed tracing, metrics and logs can additionally be exported to **Azure
Application Insights**. This is opt-in and configured at startup via
`observability.setup()`, which enables the Microsoft Agent Framework's OpenTelemetry
instrumentation when a connection string is present:

| Variable | Example | Description |
| --- | --- | --- |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | `InstrumentationKey=...;IngestionEndpoint=...` | Azure Application Insights connection string. When unset, telemetry is silently skipped — no Azure resources are needed for local development. |

Each `/ask` request appears as one App Insights *operation* (the FastAPI request span,
auto-instrumented by `configure_azure_monitor`). The LLM calls in the plan → build →
generate pipeline are traced as `gen_ai` child spans under it:

- **The agent's planning and answer turns** are traced automatically by the
  Microsoft Agent Framework's instrumentation (they run through the MAF chat client).

There is no separate cypher-generation LLM call to trace — the query is built
deterministically from the agent's structured intent.

## Running with the UIs

`uv run poe serve` starts only the API. To start the backend together with the
Streamlit UI, use `uv run poe dev` (runs `scripts/start-dev.sh`, which also starts the
Vue frontend). In VS Code, press **F5** and choose **“Launch All (Backend + Streamlit
+ Frontend)”** to launch everything at once.

## Stack

- **Python 3.13+** with **FastAPI** and **uvicorn**
- **Neo4j** for graph storage and Cypher queries
- **Microsoft Agent Framework** (agentic planning + answer generation) backed by
  **Azure OpenAI**; queries are built deterministically from the agent's structured intent
  (no text-to-Cypher).
- **uv** for dependency management, **poethepoet** for task running
- **ruff** for linting/formatting, **mypy** for type checking, **pytest** for tests
- **Azure Application Insights** (via **azure-monitor-opentelemetry**) for optional telemetry
