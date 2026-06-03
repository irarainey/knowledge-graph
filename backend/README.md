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

### How it works

A request to `/ask` flows through a two-stage **retrieve → generate** pipeline:

```
question
   │
   ▼
┌─────────────────────────── Text2CypherRetriever ───────────────────────────┐
│ 1. Build a cypher-generation prompt from: the graph schema + few-shot       │
│    examples + the user's question.                                          │
│ 2. LLM writes a Cypher query.                                               │
│ 3. EXPLAIN the query; reject it unless Neo4j reports it as read-only.        │
│ 4. Run the query (READ routing) and format each row as JSON.                │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                    │ rows (context) + the generated Cypher
                                    ▼
┌──────────────────────────────────── GraphRAG ───────────────────────────────┐
│ 5. Build an answer prompt from the rows (context) + the question.            │
│ 6. LLM writes the final natural-language answer.                            │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                    ▼
              AskResponse { answer, cypher_used, records }
```

Step by step, in `src/agent.py`:

1. **Startup (`KnowledgeGraphAgent.from_settings`).** The agent opens its own
   **synchronous** Neo4j driver (the `neo4j-graphrag` retrievers are sync-only,
   separate from the async driver that backs `/query`), builds the LLM client
   (`build_llm`), introspects the schema once (`fetch_schema_text`), and wires up a
   `Text2CypherRetriever` and a `GraphRAG` pipeline.

2. **Schema introspection (`fetch_schema_text`).** Read-only Cypher queries collect
   node labels with their properties, the relationship types that connect labels, and
   relationship properties (no APOC). Each property is rendered with an inferred **type
   and example value** (e.g. `Flight: date (str, e.g. "2026-05-20")`) so the LLM can
   see, for instance, that dates are ISO strings that need casting. Generic
   super-labels shared across many node types (e.g. `System`, `Component`,
   `FlightPhase`) are trimmed to property names only to keep the prompt focused.

3. **Cypher generation.** When a question arrives, the retriever fills its
   `CYPHER_GENERATION_PROMPT` — a domain-tuned prompt — with the schema, the few-shot
   `DEFAULT_EXAMPLES` (question → Cypher pairs), and the question, then asks the LLM for
   a single Cypher query. The prompt encodes the rules that make queries actually
   return data: cast ISO-string dates with `date()` on both sides, inline literals
   (never use `$parameters`, which the retriever does not supply), traverse the
   `Aircraft → System → Component → Part` hierarchy in full for component/part
   questions (but query `:Flight` directly for flight/hours/date questions), use
   `coalesce()`/`IS NOT NULL` for nullable numerics, never invent property names, and
   aggregate for count/sum/total questions.

4. **Read-only enforcement.** Before executing, the retriever runs `EXPLAIN` on the
   generated query and refuses anything Neo4j does not classify as read-only. The
   query then executes with READ routing. This is a database-level guarantee — the
   model cannot mutate the graph even if it emits `CREATE`/`DELETE`/`SET`.

5. **Row formatting (`_record_to_item`).** Each returned record is converted to a
   JSON-serialisable dict (via `to_jsonable`, which unpacks nodes/relationships into
   property maps). The JSON becomes the LLM's context; the structured dict is kept
   so the API can return the raw `records`.

6. **Answer generation.** `GraphRAG` fills the custom `RAG_TEMPLATE` with the rows
   (context) and the question, and the LLM writes the answer. The template instructs
   the model to answer **only** from the retrieved rows, to say so when the data has
   no answer, and to report numbers **exactly** (no rounding/reformatting).

7. **Response assembly (`ask`).** Because the pipeline is synchronous, `ask` runs it
   in a worker thread (`asyncio.to_thread`) so the FastAPI event loop stays
   responsive; the shared sync driver is thread-safe for concurrent queries. The
   result is packed into `AskResult(answer, cypher_used, records)`. If cypher
   generation/execution fails (e.g. `Text2CypherRetrievalError`) or any LLM/network
   error occurs, `/ask` **degrades gracefully** with a plain "couldn't find an
   answer" message instead of returning a 500.

The two LLM calls (cypher generation and answer generation) share a single LLM
client. Built-in rate-limit handling (retry with exponential backoff) is provided by
`neo4j-graphrag`.

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
  "cypher_used": ["MATCH (ac:Aircraft)-[:HAS_SYSTEM]->(:System)-[:HAS_COMPONENT]->(e:PistonEngine) MATCH (f:Flight)-[:USES_AIRCRAFT]->(ac) WHERE date(f.date) >= date('2026-05-25') RETURN e.name AS engine, count(f) AS flights, sum(coalesce(f.flightTime_hours, 0)) AS hours"],
  "records": [{"engine": "Lycoming IO-360", "flights": 4, "hours": 2.2}]
}
```

## Stack

- **Python 3.13+** with **FastAPI** and **uvicorn**
- **Neo4j** for graph storage and Cypher queries
- **neo4j-graphrag** (text-to-Cypher GraphRAG) with **Azure OpenAI** for LLM calls
- **uv** for dependency management, **poethepoet** for task running
- **ruff** for linting/formatting, **mypy** for type checking, **pytest** for tests
