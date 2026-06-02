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

## Stack

- **Python 3.13+** with **FastAPI** and **uvicorn**
- **Neo4j** for graph storage and Cypher queries
- **Azure OpenAI** (via agent-framework-openai) for LLM calls
- **uv** for dependency management, **poethepoet** for task running
- **ruff** for linting/formatting, **mypy** for type checking, **pytest** for tests
