# Knowledge Graph

A proof-of-concept for displaying and querying knowledge graphs. Users explore a visual knowledge graph in the browser and ask natural-language questions that are answered via knowledge-graph-augmented RAG (Retrieval-Augmented Generation) powered by Azure OpenAI.

## Architecture

```
┌───────────────┐       ┌───────────────┐       ┌───────────────┐
│   Frontend    │◄─────►│   Backend     │◄─────►│    Neo4j      │
│  Vue / TS     │  API  │  FastAPI      │ Bolt  │   (container) │
│  (pnpm)       │       │  (uv)         │       │               │
└───────────────┘       └───────┬───────┘       └───────────────┘
                                │
                                ▼
                        ┌───────────────┐
                        │ Azure OpenAI  │
                        └───────────────┘
```

- **Frontend** — Vue 3 / TypeScript SPA that renders knowledge graphs and provides a query interface. Uses pnpm for package management.
- **Backend** — Python FastAPI service that queries Neo4j for graph-based retrieval, then calls Azure OpenAI to generate answers (knowledge RAG). Uses uv for package management.
- **Neo4j** — Graph database running as a Docker container, storing knowledge graph nodes and relationships.

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

# Frontend
cd frontend
pnpm install
```

See [backend/README.md](backend/README.md) and [frontend/README.md](frontend/README.md) for component-specific details.

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