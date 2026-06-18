# Chat UI (Streamlit)

A chat-style Streamlit front end for the Knowledge Graph **`/ask`** endpoint, titled
for the modelled aircraft (**Cessna 172S Skyhawk — G-ECHO**). Type a natural-language
question and the backend plans a typed query over the graph, runs it, and generates the
answer with a Microsoft Agent Framework agent, **streamed token-by-token**. Each answer
also shows the Cypher it ran and the graph rows it retrieved.

This is a thin HTTP client — all retrieval and reasoning live in the
[`backend`](../../backend). It consumes the streaming endpoint `POST /ask`
(newline-delimited JSON), falling back to a clear message if the backend is
unreachable or unconfigured.

## Prerequisites

The backend must be running and reachable:

```bash
cd backend
uv run poe serve          # starts FastAPI on http://localhost:8080
```

`/ask` also requires Azure OpenAI credentials in `backend/.env` (see
`backend/.env.example`). Without them the endpoint returns 503 and the UI shows a
clear message.

## Run

```bash
cd frontend/chat-ui
uv sync
uv run streamlit run app.py
```

Streamlit opens on <http://localhost:8501>.

To start the backend, this UI **and** the Vue frontend together, press **F5** in VS
Code and pick **“Launch All (Backend + Streamlit + Frontend)”**, or run
`scripts/start-dev.sh` from the repo root (equivalently, `cd backend && uv run poe
dev`).

## Configuration

The chat UI works with **no `.env`** when everything runs on the default localhost
ports — every setting has a built-in default. Copy `.env.example` to `.env` only if you
need to override a URL (e.g. the backend or Neo4j runs elsewhere):

```bash
cp .env.example .env
```

The chat UI needs no secrets of its own; Azure OpenAI credentials live in `backend/.env`.

| Variable | Default | Description |
| --- | --- | --- |
| `BACKEND_URL` | `http://localhost:8080` | Base URL of the FastAPI service exposing `/ask`. |
| `FRONTEND_URL` | `http://localhost:5173` | URL of the Vue graph renderer, opened from the sidebar. |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection — its **host** is used to build the Neo4j Browser link (HTTP port `7474`). |
| `NEO4J_BROWSER_URL` | _(derived)_ | Override for the full Neo4j Browser URL (e.g. for remote/Aura instances). |

## Features

- Chat-style history of questions and answers.
- **Live token streaming** with a "thinking" status indicator that reflects the
  current pipeline phase from the backend's `progress` events: *Deciding what to fetch…*
  → *Building the query…* → *Querying the graph database…* → *Generating the answer…*,
  then it clears so the answer sits at the top.
- A single **Debug details** expander per answer, laid out as the request **workflow**
  in chronological order so it reads top-to-bottom like the steps the agent ran:
  1. **Agent planning** — the agent's planning LLM turn, where it decides what to fetch
     and emits a typed query intent, with timing/tokens and the request in a collapsible
     panel (collapsed by default).
  2. **Build and run the query** — the query is built deterministically from the intent
     (no LLM call) and run against Neo4j; shows the Cypher and the graph-query duration
     and the retrieved rows.
  3. **Generate the answer from the retrieved data** — the agent's answer LLM turn, with
     the answer prompt (system instructions + your question) in a collapsible panel; the
     retrieved rows are supplied to the model separately as the tool result.

  A **Summary** at the bottom lists the model, LLM-call count (**2** for an on-topic
  question: planning + answer — the query is built deterministically with no LLM call), a
  token table
  (prompt/completion/total per call) and a timings table. All telemetry comes from a
  `stats` event the backend emits on the stream. The panel also shows **who the answer
  was generated for** — the acting identity, its role and clearance, and the access
  policy version — taken from the `stats` event's `principal`, and an **Audit** slice
  (outcome, timestamp, schema fingerprint, policy version, and any authorization or
  query-safety denials)
  from the `stats` event's `audit` record. It also shows a **Snapshot** line — whether the
  answer was grounded in the *current* data or *as of* a chosen date, whether a temporal
  filter actually applied, and the active ontology version — from the `stats` event's
  `versioning` block.
- An **“Ask as” identity selector** in the sidebar, populated from the backend's
  `GET /users` (so it is policy-driven, not hard-coded). The chosen identity is sent with
  each question; the backend resolves it into a principal and attributes the answer to it.
  Switching identity **starts a new conversation**, because the chat history is part of the
  model's context and must not carry across an identity change. If the backend is
  unreachable, the selector falls back to a single least-privilege identity so the page
  still loads.
- A **Snapshot** control in the sidebar: answer from the **current** graph (default) or
  **as of** a chosen date. When set, it sends an `as_of` date with each question and the
  backend answers from the graph **as it existed on that date** — the version of each
  versioned entity that was valid then, and only the events (flights) that had already
  occurred.
- Sidebar with a **New conversation** button, one-click **example questions** (spanning the
  operational aircraft graph and the engineering/SDLC graph plus cross-domain seams between
  them — the engineering and cross-domain examples need the **Software Engineer** identity),
  and shortcut buttons to **open the graph renderer** (Vue app) and **open the Neo4j
  browser** in a new tab.
