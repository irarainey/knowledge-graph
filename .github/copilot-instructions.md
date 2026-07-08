# Copilot Instructions

Read [`CONTEXT.md`](../CONTEXT.md) first, every session. It is the only
always-loaded file — do not ask the user for anything it already states.

Open [`ARCHITECTURE.md`](../ARCHITECTURE.md), [`DECISIONS.md`](../DECISIONS.md),
the ADRs under `docs/adr/`, or component READMEs **only when the current task
needs them** — never up front, never all of them together. This is what keeps
context small; do not treat the links in CONTEXT.md as an instruction to load
everything.

Anything tagged **CRITICAL** in CONTEXT.md is binding. If a requested change
would contradict a CRITICAL constraint, stop and flag it — do not work around it.

## Conventions (reference — reconstructable from repo config)
- Backend: Python 3.13+, uv, ruff + mypy (`backend/pyproject.toml`), pytest
  with `asyncio_mode = "auto"`. Commands: `uv run poe lint|format|test` from
  `backend/`.
- Frontend (graph renderer): Vue 3 Composition API, TypeScript strict,
  **pnpm only** (never npm/yarn). Commands: `pnpm install|dev|build|lint`
  from `frontend/graph-renderer/`.
- Design principles: SOLID, YAGNI, KISS; separation of concerns between API
  routes, business logic, data access, and LLM integration.
