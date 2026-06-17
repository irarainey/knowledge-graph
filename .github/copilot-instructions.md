# Copilot Instructions

## Design Principles

- **SOLID, YAGNI, KISS** — never build more than is needed. Keep code simple and maintainable.
- **Separation of concerns** — each module has a single responsibility. API routes, business logic, data access, and LLM integration live in separate layers.
- **Clean code** — favour readability over cleverness. Small functions, clear naming, minimal nesting.

## Architecture

This is a knowledge graph PoC with four components:

- **Graph renderer** (`frontend/graph-renderer/`) — Vue 3 / TypeScript SPA using pnpm. Renders the knowledge graph from the static `data/aircraft-knowledge-graph.json` export (no backend calls).
- **Chat UI** (`frontend/chat-ui/`) — Python chat front end using uv. Streams natural-language answers from the backend's `/ask` endpoint with a per-answer debug panel.
- **Backend** (`backend/`) — Python FastAPI service using uv. A Microsoft Agent Framework agent backed by Azure OpenAI answers questions: it emits a typed query intent that the backend validates against the caller's access policy and turns into a parameterised, read-only Cypher query deterministically (no text-to-Cypher), then generates the answer from the retrieved rows.
- **Neo4j** — Graph database running as a Docker container. Stores knowledge graph nodes and relationships with Cypher queries.

## Backend

### Setup & Commands

All commands run from the `backend/` directory:

```bash
uv sync                  # install all dependencies
uv run poe lint          # ruff check + format check + mypy
uv run poe format        # auto-fix lint issues and format
uv run poe test          # run all tests (pytest -v)
uv run poe lint:ruff     # ruff only
uv run poe lint:mypy     # mypy only
```

Run a single test:

```bash
uv run pytest tests/test_foo.py::test_bar -v
```

### Conventions

- Python 3.13+, line length 140 characters.
- **Logging, not `print()`** — log through the application logger via `get_logger(__name__)` (root logger `kg`, configured by `setup_logging()` in `common/logging_config.py`). `LOG_LEVEL` (default `INFO`) controls verbosity. Optional Azure Application Insights telemetry is wired up by `common/observability.py` (`setup()`), enabled when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set.
- Ruff rules: isort (`I`), pyupgrade (`UP`), bugbear (`B`), simplify (`SIM`), pep8-naming (`N`). `N815` is ignored for Pydantic models matching JSON APIs.
- Async-first: pytest uses `asyncio_mode = "auto"` — no markers needed on async tests.
- Source code in `backend/src/`, tests in `backend/tests/`.

## Frontend

The `frontend/` directory holds two front ends: the Vue **graph renderer**
(`frontend/graph-renderer/`) and the Streamlit **chat UI** (`frontend/chat-ui/`, a
uv/Python project — see the chat UI's own README).

### Setup & Commands

All commands below run from the `frontend/graph-renderer/` directory:

```bash
pnpm install             # install dependencies
pnpm dev                 # start dev server
pnpm build               # production build
pnpm lint                # lint and format check
```

### Conventions

- Vue 3 with Composition API (`<script setup lang="ts">`).
- TypeScript strict mode.
- Components, composables, and types in separate directories.
- **Always use pnpm** — never use npm or yarn. All Node commands should use `pnpm` (e.g. `pnpm install`, `pnpm add`, `pnpm dev`).
