# Copilot Instructions

## Design Principles

- **SOLID, YAGNI, KISS** — never build more than is needed. Keep code simple and maintainable.
- **Separation of concerns** — each module has a single responsibility. API routes, business logic, data access, and LLM integration live in separate layers.
- **Clean code** — favour readability over cleverness. Small functions, clear naming, minimal nesting.

## Architecture

This is a knowledge graph PoC with four components:

- **Frontend** (`frontend/`) — Vue 3 / TypeScript SPA using pnpm. Renders the knowledge graph from the static `data/knowledge-graph.json` export (no backend calls).
- **Streamlit UI** (`streamlit-ui/`) — Python chat front end using uv. Streams natural-language answers from the backend's `/ask/stream` endpoint with a per-answer debug panel.
- **Backend** (`backend/`) — Python FastAPI service using uv. Retrieves from Neo4j with text-to-Cypher (`neo4j-graphrag`), then generates answers with a Microsoft Agent Framework agent backed by Azure OpenAI.
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
- `print()` is allowed — used in logging.
- Ruff rules: isort (`I`), pyupgrade (`UP`), bugbear (`B`), simplify (`SIM`), pep8-naming (`N`). `N815` is ignored for Pydantic models matching JSON APIs.
- Async-first: pytest uses `asyncio_mode = "auto"` — no markers needed on async tests.
- Source code in `backend/src/`, tests in `backend/tests/`.

## Frontend

### Setup & Commands

All commands run from the `frontend/` directory:

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
