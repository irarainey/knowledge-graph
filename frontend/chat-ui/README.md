# Chat UI (Streamlit)

A chat-style Streamlit front end for the Knowledge Graph **`/ask`** endpoint, titled
for the modelled aircraft (**Cessna 172S Skyhawk — G-ECHO**). Type a natural-language
question and the backend retrieves from the graph (text-to-Cypher) and generates the
answer with a Microsoft Agent Framework agent, **streamed token-by-token**. Each answer
also shows the Cypher it ran and the graph rows it retrieved.

This is a thin HTTP client — all retrieval and reasoning live in the
[`backend`](../../backend). It consumes the streaming endpoint `POST /ask/stream`
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

Copy `.env.example` to `.env` and adjust as needed:

```bash
cp .env.example .env
```

| Variable | Default | Description |
| --- | --- | --- |
| `BACKEND_URL` | `http://localhost:8080` | Base URL of the FastAPI service exposing `/ask/stream`. |
| `FRONTEND_URL` | `http://localhost:5173` | URL of the Vue graph renderer, opened from the sidebar. |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection — its **host** is used to build the Neo4j Browser link (HTTP port `7474`). |
| `NEO4J_BROWSER_URL` | _(derived)_ | Override for the full Neo4j Browser URL (e.g. for remote/Aura instances). |

## Features

- Chat-style history of questions and answers.
- **Live token streaming** with a "thinking" status indicator that reflects the
  current phase: *asking the LLM for the graph data* (cypher generation) → *asking
  the LLM to generate the answer* (answer generation), then it clears so the answer
  sits at the top.
- A single **Debug details** expander per answer, laid out as the request **workflow**
  in chronological order so it reads top-to-bottom like the steps the agent ran:
  1. **Call the LLM with the graph schema** — timing/tokens, with the prompt in a
     collapsible panel (collapsed by default).
  2. **Generate the Cypher query** — the Cypher the LLM produced.
  3. **Query the graph database** — the graph-query duration and the retrieved rows.
  4. **Call the LLM with the retrieved data** — timing/tokens, with the answer prompt
     in a collapsible panel (collapsed by default).

  A **Summary** at the bottom lists the model, LLM-call count, a token table
  (prompt/completion/total per call) and a timings table. All telemetry comes from a
  `stats` event the backend emits on the stream.
- Sidebar with a **New conversation** button, one-click **example questions**, and
  shortcut buttons to **open the graph renderer** (Vue app) and **open the Neo4j
  browser** in a new tab.
