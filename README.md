# Knowledge Graph

A proof-of-concept for displaying and querying a knowledge graph of a **Cessna 172S Skyhawk** (registration **G-ECHO**). Users explore a visual knowledge graph in the browser and ask natural-language questions that are answered via knowledge-graph-augmented RAG (Retrieval-Augmented Generation) powered by Azure OpenAI.

There are two front ends:

- a **Vue** SPA that renders the interactive knowledge graph directly from the
  `data/knowledge-graph.json` export, and
- a **Streamlit** chat UI for asking natural-language questions of the backend, with
  answers **streamed** token-by-token.

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
                  └───────────────┘    │  text2cypher  │
                   POST /ask/stream    │  + MAF Agent  │
                       (NDJSON)        └───────┬───────┘
                                              │ cypher-gen + answer-gen
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

- **Frontend** — Vue 3 / TypeScript SPA that renders the knowledge graph from the
  static `data/knowledge-graph.json` export (no backend dependency). Uses pnpm. Runs on
  <http://localhost:5173>.
- **Streamlit UI** — Python chat front end for the backend's `/ask/stream` endpoint with
  live token streaming and a per-answer debug panel. Uses uv. Runs on
  <http://localhost:8501>. Sidebar buttons open the Vue graph renderer and the Neo4j
  browser in a new tab.
- **Backend** — Python FastAPI service that retrieves from Neo4j with **text-to-Cypher**
  (Neo4j's `neo4j-graphrag` package), then uses a **Microsoft Agent Framework** agent
  (backed by Azure OpenAI) to generate the answer from the retrieved rows.
  Uses uv. Runs on <http://localhost:8080>.
- **Neo4j** — Graph database running as a Docker container, storing the aircraft's nodes
  and relationships.

### Request workflow

A question asked in the Streamlit UI flows through a two-LLM **retrieve → generate**
pipeline. The same four steps are surfaced, in order, in the UI's per-answer **Debug
details** panel.

```
User ─ question ─▶ Streamlit UI ─ POST /ask/stream ─▶ Backend
                                                        │
   ┌────────────────────────────────────────────────────┘
   ▼
Step 1 · Call the LLM with the graph schema
         Backend ──(schema + examples + question)──▶ Azure OpenAI
Step 2 · LLM returns a Cypher query
         Azure OpenAI ──(Cypher)──▶ Backend
Step 3 · Query the graph database
         Backend ──(EXPLAIN read-only, then run Cypher)──▶ Neo4j ──(rows)──▶ Backend
         Backend ──(metadata event: cypher_used, records)──▶ Streamlit UI
Step 4 · Call the LLM with the retrieved rows
         Backend ──(rows as context + question)──▶ Azure OpenAI
         Azure OpenAI ──(answer tokens)──▶ Backend ──(token events)──▶ Streamlit UI
Finally  Backend ──(stats event: tokens, durations)──▶ Streamlit UI ─▶ Debug details
         Backend ──(done event)──▶ Streamlit UI
```

While this runs, the Streamlit status indicator moves from *asking the LLM for the
graph data* (steps 1–3) to *asking the LLM to generate the answer* (step 4), then
clears so the answer sits at the top with the Debug details panel below it.

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

# Streamlit UI
cd streamlit-ui
uv sync

# Frontend
cd frontend
pnpm install
```

See [backend/README.md](backend/README.md) and [streamlit-ui/README.md](streamlit-ui/README.md) for component-specific details.

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

The backend answers natural-language questions with a two-stage **retrieve → generate**
pipeline: Neo4j's
[`neo4j-graphrag`](https://neo4j.com/docs/neo4j-graphrag-python/current/) package handles
**text-to-Cypher** retrieval — it reads the live graph schema, has the LLM write a
read-only Cypher query, and runs it to fetch context — then a **Microsoft Agent Framework**
agent generates the answer from those rows.
Set the `AZURE_OPENAI_*` variables in `backend/.env` (see `backend/.env.example`) to
enable it.

```bash
curl -X POST http://localhost:8080/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "How many flying hours has the engine had since 2026-05-25?"}'
```

```json
{
  "answer": "Since 2026-05-25, the engine has had 2.2 flying hours across 4 flights.",
  "cypher_used": ["MATCH (e:PistonEngine) MATCH (f:Flight) WHERE f.date >= $since RETURN ..."],
  "records": [{"engine": "Lycoming IO-360", "flying_hours": 2.2, "flights": 4}]
}
```

The generated queries run in a read transaction, so they can never modify the graph.
See [backend/README.md](backend/README.md) for configuration and response details.

### Streaming answers

`POST /ask/stream` returns the same result as a stream of newline-delimited JSON
(NDJSON) events so answers can be rendered token-by-token: one `metadata` event (the
Cypher and rows), many `token` events, a `stats` event (debug telemetry — tokens and
per-step durations), then a final `done` event. The Streamlit UI consumes this endpoint
to stream answers live. See [backend/README.md](backend/README.md#post-askstream-streaming)
for the full event table.

## Streamlit chat UI

The [`streamlit-ui`](streamlit-ui) project is a chat front end for `/ask/stream` with
live token streaming. While a question is in flight, a status indicator reflects the
current step (asking the LLM for the graph data → asking the LLM to generate the
answer). Each answer carries a single **Debug details** panel that lays the request out
as the four-step workflow above — the LLM prompts (in collapsed panels), the generated
Cypher, the retrieved rows, and a summary of tokens and timings. Sidebar buttons open
the Vue graph renderer and the Neo4j browser. Run it on its own with:

```bash
cd streamlit-ui
uv sync
uv run streamlit run app.py    # http://localhost:8501
```

See [streamlit-ui/README.md](streamlit-ui/README.md) for details.