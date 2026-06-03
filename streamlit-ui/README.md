# Streamlit UI

A chat-style Streamlit front end for the Knowledge Graph **`/ask`** endpoint, titled
for the modelled aircraft (**Cessna 172S Skyhawk — G-ECHO**). Type a natural-language
question and the backend's text-to-Cypher GraphRAG agent answers it, **streamed
token-by-token**. Each answer also shows the Cypher it ran and the graph rows it
retrieved.

This is a thin HTTP client — all retrieval and reasoning live in the
[`backend`](../backend). It consumes the streaming endpoint `POST /ask/stream`
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
cd streamlit-ui
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
- **Live token streaming** with a "thinking" status indicator that moves from
  *querying the knowledge graph* → *generating answer* → *done*.
- Per-answer expanders showing the **Cypher used** and the **retrieved rows**.
- Sidebar with a **New conversation** button, one-click **example questions**, and
  shortcut buttons to **open the graph renderer** (Vue app) and **open the Neo4j
  browser** in a new tab.
