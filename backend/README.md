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

`scripts/import_graph.py` loads `data/knowledge-graph.json` into a running Neo4j
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

> ⚠️ The endpoint executes any Cypher it is given (including writes/deletes). It is
> intended for trusted, local PoC use — do not expose it publicly without adding
> authentication and query restrictions.

## Natural-language questions (agentic MAF + text-to-Cypher tool)

`src/agents/knowledge_graph_agent.py` answers natural-language questions over the graph
with a single [**Microsoft Agent Framework**](https://github.com/microsoft/agent-framework)
`Agent` that owns orchestration and is given **one tool**, `search_knowledge_graph`. The
tool wraps Neo4j's [`neo4j-graphrag`](https://neo4j.com/docs/neo4j-graphrag-python/current/)
package in a **text-to-Cypher** pattern: a `Text2CypherRetriever` asks the LLM to write a
Cypher query from the question and the live graph schema, validates that it is **read-only**
(via `EXPLAIN`), and runs it. The agent is **forced** to call this tool on its first turn,
then writes the final answer from the rows it gets back.

Forcing the tool preserves the grounding guarantee of a deterministic retrieve→generate
pipeline (the agent only ever answers from rows it actually retrieved) while moving
orchestration into native MAF, making it straightforward to add further tools later.
Read-only execution is enforced by `neo4j-graphrag` itself, so the model can never modify
the graph even if it generates a write.

Before any retrieval or LLM call, a **deterministic relevance guardrail** (`common/guardrails.py`,
**no extra LLM call**) rejects off-topic questions. Off-topic questions return a fixed
refusal message with empty metadata and zero LLM usage.

The graph schema is introspected with plain Cypher (no APOC required), so it works
against a stock Neo4j Community container. The schema is read once at startup —
**restart the backend after re-importing the graph** so the agent picks up changes.

### How it works

A request to `/ask` flows through the relevance guardrail, then a single MAF agent that
is forced to retrieve once via its tool before answering:

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
┌──────────────────────────────── MAF Agent ──────────────────────────────────┐
│ Turn 1 — LLM call #1 (tool-planning): forced to call search_knowledge_graph. │
│   ┌──────────────────── Text2CypherRetriever (tool) ───────────────────────┐ │
│   │ LLM call #2 (cypher generation): build a prompt from schema + few-shot  │ │
│   │ examples; the LLM writes a Cypher query; EXPLAIN rejects it unless       │ │
│   │ read-only; run it (READ routing); return the rows as JSON to the agent. │ │
│   └─────────────────────────────────────────────────────────────────────────┘ │
│ Turn 2 — LLM call #3 (answer): tool choice auto; the agent writes the final  │
│ answer from the rows (delivered to the model as the tool-result message).    │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                    ▼
              NDJSON stream { metadata, token…, stats, done }
```

A single on-topic question therefore makes **three** LLM calls — the agent's
tool-planning turn, the cypher-generation call inside the tool, and the answer turn — all
surfaced individually in the `stats` event (see [`POST /ask`](#post-ask)).

Step by step, in `src/agents/knowledge_graph_agent.py`:

1. **Startup (`KnowledgeGraphAgent.from_settings`).** The agent opens its own
   **synchronous** Neo4j driver (the `neo4j-graphrag` retrievers are sync-only,
   separate from the async driver that backs `/query`), builds the retrieval LLM
   client (`build_llm`) and the Microsoft Agent Framework chat client
   (`build_chat_client`), introspects the schema once (`fetch_schema_text`), builds the
   guardrail vocabulary from that schema (`build_relevance_vocabulary`), and wires up a
   `Text2CypherRetriever` exposed as the `search_knowledge_graph` tool on a MAF `Agent`.
   The agent's `default_options` pin the tool choice to `required` so retrieval is forced
   on the first turn.

2. **Relevance guardrail (`common/guardrails.py`).** When a question arrives, it is first
   checked against a vocabulary derived from the live schema (label/relationship/property
   tokens, CamelCase-split and singularised) plus curated domain keywords, minus a
   denylist of generic tokens (`name`, `type`, `date`, …). If no question token matches,
   the request is **refused without any LLM call** — a fixed message is streamed with
   empty `metadata` and zero token usage. This is a relevance gate, **not** a
   prompt-injection defence.

3. **Schema introspection (`fetch_schema_text`).** Read-only Cypher queries collect
   node labels with their properties, the relationship types that connect labels, and
   relationship properties (no APOC). Each property is rendered with an inferred **type
   and example value** (e.g. `Flight: date (str, e.g. "2026-05-20")`) so the LLM can
   see, for instance, that dates are ISO strings that need casting. Generic
   super-labels shared across many node types (e.g. `System`, `Component`,
   `FlightPhase`) are trimmed to property names only to keep the prompt focused.

4. **Cypher generation (inside the tool).** The forced `search_knowledge_graph` tool runs
   the retriever, which fills its `CYPHER_GENERATION_PROMPT` — a domain-tuned prompt —
   with the schema, the few-shot `DEFAULT_EXAMPLES` (question → Cypher pairs), and the
   question, then asks the LLM for a single Cypher query. The prompt encodes the rules
   that make queries actually return data: cast ISO-string dates with `date()` on both
   sides, inline literals (never use `$parameters`, which the retriever does not supply),
   traverse the `Aircraft → System → Component → Part` hierarchy in full for
   component/part questions (but query `:Flight` directly for flight/hours/date
   questions), use `coalesce()`/`IS NOT NULL` for nullable numerics, never invent
   property names, and aggregate for count/sum/total questions.

5. **Read-only enforcement.** Before executing, the retriever runs `EXPLAIN` on the
   generated query and refuses anything Neo4j does not classify as read-only. The
   query then executes with READ routing. This is a database-level guarantee — the
   model cannot mutate the graph even if it emits `CREATE`/`DELETE`/`SET`.

6. **Row formatting (`record_to_item`).** Each returned record is converted to a
   JSON-serialisable dict (via `to_jsonable`, which unpacks nodes/relationships into
   property maps) by the retriever's `result_formatter` (`record_to_item` in
   `common/retrieval.py`). The JSON is returned from the tool as the agent's context; the
   structured dict is also stashed so the API can return the raw `records`. If retrieval
   raises (e.g. `Text2CypherRetrievalError`) or returns no rows, the tool returns a
   graceful message instead and the agent still answers (with empty `metadata`).

7. **Answer generation.** Once MAF resets the forced tool choice to `auto` after the
   first iteration, the agent makes its second turn (LLM call #3) and writes the answer
   from the tool's rows. Its system prompt
   (`AGENT_SYSTEM_PROMPT`) instructs it to always call the tool, answer **only** from the
   retrieved rows, say so when the data has no answer, report numbers **exactly** (no
   rounding/reformatting), and reply in **plain text** (no markdown/formatting).

8. **Response assembly (`ask`).** The synchronous retrieval inside the tool runs in a
   worker thread (`asyncio.to_thread`) so the FastAPI event loop stays responsive; the
   shared sync driver is thread-safe for concurrent queries. The retrieved Cypher and
   rows are captured via a per-request `retrieval_sink` and emitted up front as a
   `metadata` event, then the MAF agent is streamed natively
   (`self._agent.run(..., stream=True)`) so answer tokens are forwarded as they arrive,
   followed by a `stats` and a `done` event. A `_MafTurnRecorder` chat middleware records
   each of the agent's two LLM turns (planning + answer) individually — MAF otherwise
   aggregates their token usage — so the `stats` event can report all three calls
   separately. If a generation/network error occurs, `/ask` **degrades gracefully** — it
   emits an in-band `error` event instead of returning a 500.

All **three** LLM calls (the agent's tool-planning turn, cypher generation inside the
tool, and the agent's answer turn) target the same Azure OpenAI deployment: cypher
generation uses the `neo4j-graphrag` LLM client and the agent's two turns use the
Microsoft Agent Framework chat client. Built-in rate-limit handling
(retry with exponential backoff) is provided by `neo4j-graphrag` for the retrieval call.

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
| `AZURE_OPENAI_TEMPERATURE` | `0` | Optional. Temperature for text-to-Cypher generation; pin to `0` for more deterministic, reproducible queries. Omitted when unset — leave unset for models that reject a non-default temperature. |

### `POST /ask`

The question pipeline streams the answer as the LLM generates it. The request body is:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `question` | string | yes | A natural-language question about the graph. |

The response is `application/x-ndjson` — a stream of newline-delimited JSON events,
one object per line:

| Event | Shape | When |
| --- | --- | --- |
| `progress` | `{ "type": "progress", "phase": "planning" \| "cypher" \| "querying" \| "answering" }` | Repeated, as the pipeline advances through its stages — so the client can show which step is in flight. Skipped for off-topic questions. |
| `metadata` | `{ "type": "metadata", "cypher_used": [...], "records": [...] }` | Once, after retrieval, before any tokens. |
| `token` | `{ "type": "token", "text": "..." }` | Repeated, as answer tokens arrive. |
| `error` | `{ "type": "error", "message": "..." }` | Only on failure (in-band, since headers are already sent). |
| `stats` | `{ "type": "stats", "model": ..., "llm_calls": N, "tokens": {...}, "calls": [...], "durations_ms": {...}, "cypher_count": N, "record_count": N }` | Once, just before `done` — debug/telemetry for the request. |
| `done` | `{ "type": "done" }` | Always last. |

The `stats` event reports debug telemetry for the request: the model, the number of
LLM calls (agent tool-planning + cypher-generation + answer-generation = 3 for an
on-topic question), aggregated token usage with a
per-call breakdown, and timings. Each entry in `calls` carries its own `duration_ms`,
and `durations_ms` reports `retrieval`, `graph_query` (the Neo4j execution, i.e.
retrieval minus the cypher-generation LLM call), `generation` and `total`. Token and
duration fields per call let the UI show how long each LLM call and the graph query
took. The agent's planning and answer tokens are captured per-turn by the
`_MafTurnRecorder` middleware (MAF's response otherwise aggregates them); cypher-generation
tokens and timing are captured by wrapping the retriever's internal `llm.invoke` call.
Token fields are `null` when usage is not reported.

The `progress` events let the client surface the pipeline stage currently in flight.
The cypher-generation and graph-query steps run inside the forced tool and emit no
answer tokens, so without these events the UI would stall on one label for seconds.
Each stage calls a per-request progress callback at its boundary (`planning` up front,
then `cypher`, `querying` and `answering`); `ask` merges those onto the response stream
alongside the answer tokens (the cypher recorder runs in a worker thread, so its
progress is marshalled back onto the event loop). The phases mirror the four steps in
the chat UI's debug panel.

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
  -d '{"question": "How many flying hours has the engine had since 2026-05-25?"}'
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
{"type": "stats", "model": "gpt-5.4", "llm_calls": 3, "tokens": {"prompt": 10944, "completion": 106, "total": 11050}, "calls": [{"stage": "agent_planning", "prompt": 277, "completion": 39, "total": 316, "duration_ms": 720.3}, {"stage": "cypher_generation", "prompt": 10390, "completion": 28, "total": 10418, "duration_ms": 1850.4}, {"stage": "answer_generation", "prompt": 277, "completion": 39, "total": 316, "duration_ms": 1269.9}], "durations_ms": {"retrieval": 1859.5, "graph_query": 9.1, "generation": 1269.9, "total": 3849.8}, "cypher_count": 1, "record_count": 6}
{"type": "done"}
```

The [`frontend/chat-ui`](../frontend/chat-ui) project consumes this endpoint to stream answers
into a chat interface.

## Evaluation

`uv run poe evaluate` runs an **offline, deterministic** evaluation of the `/ask`
pipeline against a pre-baked ground-truth file — **no LLM-as-judge**. It drives the
in-process `KnowledgeGraphAgent` (the same code path the API uses), so it needs the
same Neo4j and Azure OpenAI settings in `backend/.env`, and the graph must already be
imported.

```bash
uv run poe evaluate                                   # eval/ground_truth.json -> eval/results/eval-<timestamp>.json
uv run python scripts/evaluate.py --tag aggregation   # only questions tagged "aggregation"
uv run python scripts/evaluate.py --question-id flight-count --output -   # one question, report to stdout
uv run python scripts/evaluate.py --f1-threshold 0.7  # stricter pass bar
```

### Ground truth

[`eval/ground_truth.json`](eval/ground_truth.json) holds a list of questions, each with
a hand-written **gold Cypher** query (validated against the imported data) and optional
`tags`, `expected_answer_values`, and `answer_key`:

```json
{
  "id": "powerplant-components",
  "question": "What components make up the powerplant system?",
  "tags": ["multi-hop", "list"],
  "gold_cypher": "MATCH (:PowerplantSystem)-[:HAS_COMPONENT]->(c:Component) RETURN DISTINCT c.name AS component ORDER BY component",
  "answer_key": "component",
  "expected_answer_values": ["Propeller", "Exhaust"]
}
```

`answer_key` (optional) pins the gold side of the retrieval comparison to a single
column — useful when the natural query returns extra descriptive columns that the
generated query may or may not include.

### Metrics

For each question the script runs the gold Cypher to get the expected rows, drives the
agent to get its generated Cypher, retrieved rows and streamed answer (from the
`metadata`/`token`/`stats` debug events), then computes:

- **Retrieval — value-based (primary)**: precision, recall, **F1**, Jaccard and
  exact-match comparing the *set of cell values* the agent retrieved against the gold
  rows, **ignoring column names** and **tolerating float formatting** (rounded to
  `--round-digits`, default `3`). This is the realistic measure: text-to-Cypher is
  non-deterministic and aliases columns, reorders results and rounds differently
  run-to-run, so comparing whole rows verbatim would punish correct answers. A wrong
  *value* (e.g. weight in lb instead of kg) still counts as a miss. A question *passes*
  when its value F1 ≥ `--f1-threshold` (default `0.5`).
- **Retrieval — strict (secondary diagnostic)**: `strict_exact_match` / `strict_f1`
  compare whole rows verbatim (column names, every column, exact formatting). Reported
  for insight into how much the generated Cypher's *shape* drifts from the gold; not used
  for pass/fail.
- **Answer** (string-matched, no judge): **coverage** — fraction of
  `expected_answer_values` present in the answer text; **groundedness** — fraction of
  those values also present in the retrieved rows (values stated in the answer but absent
  from the rows are flagged as a hallucination signal).
- **Operational**: Cypher **validity** rate, **empty-retrieval** rate, and token/latency
  **cost** taken from the pipeline's own `stats` event.

The metric functions live alongside the CLI in
[`scripts/evaluate.py`](scripts/evaluate.py) and are unit-tested in
[`tests/test_evaluation.py`](tests/test_evaluation.py).

### Output

A single JSON report is written to `eval/results/` (git-ignored) containing run metadata,
an overall `summary`, a per-tag breakdown (`by_tag`), and full per-question detail
(both Cypher queries, both row counts, all metrics, the answer text and the cost). A
compact summary is also logged at the end of the run.

### Dashboard

[`eval/dashboard.html`](eval/dashboard.html) is a dependency-free HTML dashboard that
loads **every** report in `eval/results/`, showing summary cards, a metric trend across
runs, the per-tag breakdown and expandable per-question detail. A static file cannot list
a directory over `file://`, so serve the folder:

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
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). `DEBUG` adds per-step pipeline detail (schema fetch, cypher generation, retrieval, generation, query execution). |

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
auto-instrumented by `configure_azure_monitor`). The LLM calls in the retrieve →
generate pipeline are traced as `gen_ai` child spans under it:

- **The agent's tool-planning and answer turns** are traced automatically by the
  Microsoft Agent Framework's instrumentation (they run through the MAF chat client).
- **Cypher generation** runs through `neo4j-graphrag`'s own OpenAI client, which the
  MAF instrumentation does not see, so it is traced with an explicit `gen_ai` span
  emitted from the agent's `invoke` wrapper (`_install_usage_recorder`). Without this,
  only the agent's own calls would be visible.

## Running with the UIs

`uv run poe serve` starts only the API. To start the backend together with the
Streamlit UI, use `uv run poe dev` (runs `scripts/start-dev.sh`, which also starts the
Vue frontend). In VS Code, press **F5** and choose **“Launch All (Backend + Streamlit
+ Frontend)”** to launch everything at once.

## Stack

- **Python 3.13+** with **FastAPI** and **uvicorn**
- **Neo4j** for graph storage and Cypher queries
- **neo4j-graphrag** (text-to-Cypher retrieval) + **Microsoft Agent Framework**
  (answer generation), both backed by **Azure OpenAI**
- **uv** for dependency management, **poethepoet** for task running
- **ruff** for linting/formatting, **mypy** for type checking, **pytest** for tests
- **Azure Application Insights** (via **azure-monitor-opentelemetry**) for optional telemetry
