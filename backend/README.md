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
```

Run a single test:

```bash
uv run pytest tests/test_example.py::test_name -v
```

## Importing the knowledge graph into Neo4j

`src/import_graph.py` loads `data/knowledge-graph.json` into a running Neo4j
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

The import upserts by default — nodes are matched on their `id` property and
relationships on the (start node, type, end node) triple — so re-running is safe.
Because upsert only adds and overwrites, use `--clear` after removing or renaming
nodes/relationships to get a clean reload.

Additional flags:

- `--file <path>` — import a different JSON export (defaults to `data/knowledge-graph.json`).
- `--env-file <path>` — load Neo4j settings from a specific `.env` file.

A convenience wrapper, `scripts/import-data.sh`, runs the same command from any
directory and forwards these arguments.

## Querying the graph (REST API)

`src/api.py` is a FastAPI service that runs arbitrary Cypher queries against the
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

> ⚠️ The endpoint executes any Cypher it is given (including writes/deletes). It is
> intended for trusted, local PoC use — do not expose it publicly without adding
> authentication and query restrictions.

## Natural-language questions (GraphRAG agent)

`src/agent.py` answers natural-language questions over the graph using Neo4j's
[`neo4j-graphrag`](https://neo4j.com/docs/neo4j-graphrag-python/current/) package
in a **text-to-Cypher** pattern. A `Text2CypherRetriever` asks the LLM to write a
Cypher query from the question and the live graph schema, validates that it is
**read-only** (via `EXPLAIN`), runs it, and feeds the rows to `GraphRAG`, which
generates the final answer.

Read-only execution is enforced by the package itself, so the model can never
modify the graph even if it generates a write.

The graph schema is introspected with plain Cypher (no APOC required), so it works
against a stock Neo4j Community container. The schema is read once at startup —
**restart the backend after re-importing the graph** so the agent picks up changes.

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

### `POST /ask`

Request body:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `question` | string | yes | A natural-language question about the graph. |

Response body:

| Field | Type | Description |
| --- | --- | --- |
| `answer` | string | The agent's natural-language answer. |
| `cypher_used` | string[] | The Cypher queries the agent ran. |
| `records` | object[] | The graph rows it retrieved as context. |

Example:

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

## Stack

- **Python 3.13+** with **FastAPI** and **uvicorn**
- **Neo4j** for graph storage and Cypher queries
- **neo4j-graphrag** (text-to-Cypher GraphRAG) with **Azure OpenAI** for LLM calls
- **uv** for dependency management, **poethepoet** for task running
- **ruff** for linting/formatting, **mypy** for type checking, **pytest** for tests
