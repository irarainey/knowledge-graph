# Knowledge Graph

A proof-of-concept for displaying and querying a knowledge graph of a **Cessna 172S Skyhawk** (registration **G-ECHO**). Users explore a visual knowledge graph in the browser and ask natural-language questions that are answered via knowledge-graph-augmented RAG (Retrieval-Augmented Generation) powered by Azure OpenAI.

There are two front ends, both under [`frontend/`](frontend):

- a **Vue** SPA (`frontend/graph-renderer/`) that renders the interactive knowledge
  graph directly from the `data/knowledge-graph.json` export, and
- a **Streamlit** chat UI (`frontend/chat-ui/`) for asking natural-language questions of
  the backend, with answers **streamed** token-by-token.

## Architecture

```
              ┌──────────────────────────────────────────────┐
              │       data/knowledge-graph.json (export)     │
              └───────────────┬───────────────┬──────────────┘
       fetch /data (static)   │               │  scripts/import-data.sh
                              ▼               ▼
                  ┌───────────────┐    ┌───────────────┐
                  │  Frontend     │    │    Neo4j      │
                  │  Vue / TS     │    │  Bolt  :7687  │
                  │  renderer     │    │  HTTP  :7474  │
                  │  :5173        │    └───────▲───────┘
                  └───────────────┘            │ Bolt (read-only Cypher)
                                               │
                  ┌───────────────┐    ┌───────┴───────┐
                  │  Streamlit UI │    │   Backend     │
                  │  chat  :8501  │──▶ │  FastAPI:8080 │
                  └───────────────┘    │  typed-intent │
                   POST /ask           │  + MAF Agent  │
                       (NDJSON)        └───────┬───────┘
                                              │ planning + answer-gen
                                              ▼
                                       ┌───────────────┐
                                       │ Azure OpenAI  │
                                       └───────────────┘
```

Both front ends are driven by the **same** `data/knowledge-graph.json`: the Vue app
fetches it directly to render the graph, while `scripts/import-data.sh` loads it into
Neo4j so the backend can retrieve from the graph. The Vue renderer is a **static client**
— it does not call the backend; only the Streamlit UI does. (The Streamlit sidebar also
links out to the Vue renderer and the Neo4j browser, opening each in a new tab.)

- **Graph renderer** (`frontend/graph-renderer/`) — Vue 3 / TypeScript SPA that renders
  the knowledge graph from the static `data/knowledge-graph.json` export (no backend
  dependency). Uses pnpm. Runs on <http://localhost:5173>.
- **Chat UI** (`frontend/chat-ui/`) — Python chat front end for the backend's
  `/ask` endpoint with live token streaming and a per-answer debug panel. Uses uv.
  Runs on <http://localhost:8501>. Sidebar buttons open the Vue graph renderer and the
  Neo4j browser in a new tab.
- **Backend** — Python FastAPI service. A single **Microsoft Agent Framework** agent
  (backed by Azure OpenAI) is given one **typed** tool: it emits a structured *query
  intent* (entity, fields, filters, optional aggregate) rather than writing Cypher. The
  backend validates that intent against the acting identity's access policy and
  **deterministically builds and runs** a parameterised, read-only Cypher query, so
  authorization is enforced outside the LLM. The agent is forced to retrieve before
  answering from the rows. A deterministic relevance guardrail (no extra LLM call) rejects
  off-topic questions up front. Uses uv. Runs on <http://localhost:8080>.
- **Neo4j** — Graph database running as a Docker container, storing the aircraft's nodes
  and relationships.

### Request workflow

A question asked in the Streamlit UI is first screened by a deterministic relevance
guardrail (no LLM call), then handled by a single **Microsoft Agent Framework** agent that
is forced to retrieve via its typed query tool before generating the answer. Because the
LLM emits a structured intent and the backend turns it into Cypher deterministically, a
question makes **two** LLM calls — agent planning and answer generation — with **no
cypher-generation LLM call**. The steps are surfaced, in order, in the UI's per-answer
**Debug details** panel.

```
User ─ question ─▶ Streamlit UI ─ POST /ask ─▶ Backend
                                                        │
   ┌────────────────────────────────────────────────────┘
   ▼
Step 0 · Relevance guardrail (no LLM)
         Backend ──(off-topic? → fixed refusal)──▶ Streamlit UI   [on-topic: continue]
Step 1 · Agent is forced to call its typed query tool  [LLM call 1: planning]
         Backend ──(policy-scoped catalog + question)──▶ Azure OpenAI
         Azure OpenAI ──(structured query intent)──▶ Backend
Step 2 · Backend builds and runs the query (no LLM)
         Backend ──(validate intent vs policy → build parameterised, read-only Cypher
                    with a clearance filter → run)──▶ Neo4j ──(rows)──▶ Backend
         Backend ──(metadata event: cypher_used, records)──▶ Streamlit UI
Step 3 · Agent generates the answer from the retrieved rows  [LLM call 2: answer]
         Backend ──(rows as tool result)──▶ Azure OpenAI
         Azure OpenAI ──(answer tokens)──▶ Backend ──(token events)──▶ Streamlit UI
Finally  Backend ──(stats event: tokens, durations)──▶ Streamlit UI ─▶ Debug details
         Backend ──(done event)──▶ Streamlit UI
```

While this runs, the backend emits `progress` events at each pipeline boundary and the
Streamlit status indicator tracks them — *Deciding what to fetch…* → *Building the
query…* → *Querying the graph database…* → *Generating the answer…* — then clears so the
answer sits at the top with the Debug details panel below it.

## Getting Started

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (for Neo4j)
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [pnpm](https://pnpm.io/) (Node package manager)
- [Node.js](https://nodejs.org/) (v24 LTS)

### Setup

```bash
# Backend
cd backend
uv sync

# Chat UI (Streamlit)
cd frontend/chat-ui
uv sync

# Graph renderer (Vue)
cd frontend/graph-renderer
pnpm install
```

See [backend/README.md](backend/README.md) and [frontend/chat-ui/README.md](frontend/chat-ui/README.md) for component-specific details.

## Running everything

Once Neo4j is running (`scripts/start-database.sh`) and `backend/.env` is configured, you can start all three apps at once.

**From VS Code (recommended):** press **F5** and choose the **“Launch All (Backend + Streamlit + Frontend)”** profile. This starts the FastAPI backend (:8080), the Streamlit UI (:8501) and the Vue frontend (:5173) together; stopping one stops them all.

**From the terminal:**

```bash
scripts/start-dev.sh
```

This launches the same three processes in the foreground — press `Ctrl-C` once to stop them all. Open the Streamlit UI at <http://localhost:8501> to ask questions. Its sidebar has two shortcut buttons that open in a new tab:

- **“Open graph renderer ↗”** — the Vue knowledge-graph view at <http://localhost:5173>.
- **“Open Neo4j browser ↗”** — the Neo4j database browser at <http://localhost:7474/browser/> (host taken from `NEO4J_URI`).

> The backend alone can be started with `cd backend && uv run poe serve`. The
> `cd backend && uv run poe dev` task is equivalent to `scripts/start-dev.sh` (it
> starts all three apps).

## Importing data into Neo4j

The knowledge graph is stored in `data/knowledge-graph.json` (a Neo4j/APOC-style
export). To load it into Neo4j:

1. Start Neo4j (if it isn't already running):

   ```bash
   scripts/start-database.sh
   ```

2. Configure the connection. Copy the example env file and adjust if needed (the
   defaults match `scripts/start-database.sh`):

   ```bash
   cp backend/.env.example backend/.env
   ```

3. Run the import:

   ```bash
   scripts/import-data.sh            # update / upsert (adds new, updates existing)
   scripts/import-data.sh --clear    # delete everything first, then import
   ```

The import is idempotent, so re-running with the default (upsert) mode is safe.
Use `--clear` after removing or renaming nodes/relationships to get a clean
reload. See [backend/README.md](backend/README.md) for the underlying command and
additional options.

## Querying the graph over HTTP

The backend exposes a FastAPI service that runs arbitrary Cypher against Neo4j.
Connection details come from `backend/.env` (the same file used by the import).

```bash
cd backend
uv run poe serve        # starts the API on http://localhost:8080 with autoreload
```

`POST /query` accepts a Cypher query plus optional parameters and returns the
result rows:

```bash
curl -X POST http://localhost:8080/query \
  -H 'Content-Type: application/json' \
  -d '{
        "query": "MATCH (a:Aircraft)-[:HAS_SYSTEM]->(:System)-[:HAS_COMPONENT]->(e:PistonEngine {name: $engineName}) MATCH (f:Flight)-[:USES_AIRCRAFT]->(a) WHERE date(f.date) >= date($since) RETURN e.name AS engine, count(f) AS flights, sum(coalesce(f.flightTime_hours, 0)) AS hours",
        "parameters": { "engineName": "Lycoming IO-360", "since": "2026-05-25" }
      }'
```

See [backend/README.md](backend/README.md) for the full request/response shape and
interactive docs.

## Asking questions in natural language

The backend answers natural-language questions with a single **Microsoft Agent Framework**
agent that is given one **typed** retrieval tool and forced to use it before answering.
Rather than writing Cypher, the agent emits a structured *query intent* (entity, fields,
filters, optional aggregate); the backend validates that intent against the acting
identity's access policy and **deterministically builds and runs** a parameterised,
read-only Cypher query, then the agent generates the answer from those rows. Because the
LLM never writes Cypher, a question makes **two** LLM calls (planning + answer), and
authorization is enforced outside the model. A deterministic relevance guardrail (no extra
LLM call) rejects off-topic questions before any retrieval. Set the `AZURE_OPENAI_*`
variables in `backend/.env` (see `backend/.env.example`) to enable it.

> Optional: set `LOG_LEVEL` (default `INFO`; use `DEBUG` for per-step pipeline detail)
> and `APPLICATIONINSIGHTS_CONNECTION_STRING` (to export OpenTelemetry traces, metrics
> and logs to Azure Application Insights). See
> [backend/README.md](backend/README.md#logging-and-observability) for details.

```bash
curl -N -X POST http://localhost:8080/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "How many flying hours has the engine had since 2026-05-25?"}'
```

`POST /ask` returns the answer as a stream of newline-delimited JSON (NDJSON)
events so it can be rendered token-by-token: one `metadata` event (the Cypher and rows),
many `token` events, a `stats` event (debug telemetry — tokens and per-step durations),
then a final `done` event.

```
{"type": "metadata", "cypher_used": ["MATCH (ac:Aircraft)-..."], "records": [{"engine": "Lycoming IO-360", "flights": 4, "hours": 2.2}]}
{"type": "token", "text": "Since "}
{"type": "token", "text": "2026-05-25"}
...
{"type": "stats", "model": "gpt-5.4", "llm_calls": 2, "tokens": {"prompt": 10944, "completion": 106, "total": 11050}, ...}
{"type": "done"}
```

The generated queries run in a read transaction, so they can never modify the graph.
The Streamlit UI consumes this endpoint to stream answers live. See
[backend/README.md](backend/README.md#post-ask) for configuration and the full
event table.

### Identity, access policy and authorization

Each question can name an **identity** (`user`) it is asked as. The backend resolves it
**server-side** — the authorization trust boundary — against an external, versioned access
policy ([`backend/policy/access-policy.json`](backend/policy/access-policy.json)) into a
principal (role, clearance, policy version) and attributes the answer to it; an unknown
or omitted id falls back to the least-privilege default (default-deny). `GET /users` lists
the selectable identities, and the chat UI's sidebar offers an **“Ask as”** selector
(switching identity starts a new conversation).

Authorization is **enforced in the backend, outside the LLM**, and is two-dimensional:
role-based **capability grants** (which entities, which sensitivity categories of field,
and whether aggregates are allowed) plus **clearance** (compared against each node's
in-graph `classification`, so by default whole classified rows stay hidden — even from a
`count` — unless a category is *clearance-gated* for that identity, in which case the row
stays visible with only the gated fields nulled).
Rather than let the model write Cypher, the agent emits a typed *query intent* that the
backend validates against the principal's policy and turns into a parameterised, read-only
query deterministically. Because the PoC runs on **Neo4j Community Edition** (no native
access control), the application is the sole enforcement boundary; the
[backend README](backend/README.md#authorization-model) documents the full model and the
Enterprise alternatives.

Every query is also **bounded and audited**: the Cypher is checked against a
deterministic safety filter (no writes, procedures, `LOAD CSV`, database switching or
schema/admin namespaces) and bounded by a per-statement timeout (`QUERY_TIMEOUT_SECONDS`)
and a row cap (`QUERY_ROW_CAP`); every answered, refused or failed request writes one
record to a `kg.audit` trail (also surfaced in the chat UI's debug panel) attributing it
to an identity, policy version and schema fingerprint.

```bash
curl http://localhost:8080/users
```

### Versioning (temporal data and ontology)

Graph data, the access policy and the ontology version on independent clocks, and an `as_of`
query reconstructs **the graph as it existed on a chosen date** (ISO `YYYY-MM-DD`) — combining
two distinct temporal concepts. A deliberately **narrow** slice (the `Specification` entity) is
**valid-time versioned**: each version is its own node sharing a `logicalId`, with
`validFrom`/`validTo` windows, a `current` flag and a `PREVIOUS_VERSION` chain — so all history
is retained, not overwritten; an as-of query selects the version valid then. `Flight` is
**event-dated** instead: each flight is an immutable event with a `date`, and an as-of query
includes only flights that had already occurred. Like the clearance filter, both temporal
filters are **injected deterministically by the backend** (for versioned / event-dated entities
only) — never written by the LLM — and the acting identity's current policy is always applied to
whichever snapshot is selected. The import asserts that each logical entity has **at most one**
current version (zero = retired). The Vue renderer shows the snapshot change as the as-of date
moves — including an illustrative **avionics retrofit** whose topology visibly changes across
eras — plus per-node history, and the chat UI's sidebar offers a snapshot date. A separate,
semantically-versioned ontology ([`backend/policy/ontology.json`](backend/policy/ontology.json))
records deprecated-but-retained terms and is attributed in each answer's telemetry. See the
[backend README](backend/README.md#versioning-temporal-data-and-ontology) for the full model
and Enterprise alternatives.

## Evaluating answer quality

The backend ships an **offline, deterministic** evaluation harness (no LLM-as-judge) that
scores the `/ask` pipeline against a pre-baked ground-truth file and writes a timestamped
JSON report per run. Run it manually with `cd backend && uv run poe evaluate`. A
dependency-free [`backend/eval/dashboard.html`](backend/eval/dashboard.html) visualizes
every report — summary cards, a metric trend across runs, a per-tag breakdown and
expandable per-question detail:

```bash
cd backend/eval && python -m http.server 8000
# then open http://localhost:8000/dashboard.html
```

See [backend/README.md](backend/README.md#evaluation) for the metrics, ground-truth
format and report schema.

## Streamlit chat UI

The [`frontend/chat-ui`](frontend/chat-ui) project is a chat front end for `/ask` with
live token streaming. While a question is in flight, a status indicator tracks the
backend's `progress` events through each step (selecting the retrieval tool → generating
the Cypher query → querying the graph database → generating the answer). Each answer
carries a single **Debug details** panel that lays the request out
as the four-step workflow above — the LLM prompts (in collapsed panels), the generated
Cypher, the retrieved rows, and a summary of tokens and timings. Sidebar buttons open
the Vue graph renderer and the Neo4j browser. Run it on its own with:

```bash
cd frontend/chat-ui
uv sync
uv run streamlit run app.py    # http://localhost:8501
```

See [frontend/chat-ui/README.md](frontend/chat-ui/README.md) for details.