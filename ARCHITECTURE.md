# ARCHITECTURE.md

## Shape
Four components, one shared data boundary:

```
data/*.json (graph exports) ──▶ Vue renderer (static, no backend calls)
                            └──▶ import scripts ──▶ Neo4j ◀── Bolt (read-only) ── Backend (FastAPI)
                                                                                        ▲
                                                                        Streamlit chat UI ── POST /ask
```

- **Graph renderer** (`frontend/graph-renderer/`) — Vue 3 / TS, pnpm. Renders
  `data/aircraft-knowledge-graph.json` overlaid with `data/sdlc-knowledge-graph.json`.
  Static client; no backend dependency.
- **Chat UI** (`frontend/chat-ui/`) — Python/Streamlit, uv. Calls the backend's
  `/ask` (streamed) and shows a per-answer debug panel.
- **Backend** (`backend/`) — FastAPI, uv. Owns the Neo4j driver, the access
  policy, the query builder, and the Microsoft Agent Framework agent.
- **Neo4j** — Docker container, Community Edition. Stores the operational
  aircraft graph and the SDLC overlay as one graph, joined by cross-domain edges.

## Data-flow rule (the trust boundary)
```
user identity (selected) → backend policy resolution → scoped tool/schema surface
   → LLM emits typed query intent → policy gates (entity/field/aggregate/clearance)
   → backend builds parameterised Cypher → Neo4j → row redaction → answer LLM
```
The LLM participates only at the two ends (intent emission, answer generation).
It never generates Cypher and never sees ungranted schema or rows.
Full mechanics: [backend/README.md → How it works](backend/README.md#how-it-works).

## Component map (deeper docs)
| Area | Where | Depth doc |
|---|---|---|
| Authorization (policy, query builder, clearance) | `backend/src/authz/` | [backend/README.md#authorization-model](backend/README.md#authorization-model) |
| Query safety & audit | `backend/src/common/query_safety.py`, `audit.py` | [backend/README.md#query-safety-and-audit](backend/README.md#query-safety-and-audit) |
| Document externalisation | `backend/src/documents/` | [backend/README.md#external-document-storage-area-4](backend/README.md#external-document-storage-area-4) |
| Versioning (as-of queries) | query builder + import script | [backend/README.md#versioning-temporal-data-and-ontology](backend/README.md#versioning-temporal-data-and-ontology) |
| SDLC/engineering domain model | `data/sdlc-knowledge-graph.json`, `data/sdlc-ontology.ttl` | [docs/sdlc-knowledge-graph-guide.md](docs/sdlc-knowledge-graph-guide.md) |
| Evaluation harness | `backend/scripts/evaluate.py`, `backend/eval/` | [backend/README.md#evaluation](backend/README.md#evaluation) |
| Neo4j Community Edition trade-offs | — | [backend/README.md#neo4j-community-edition-trade-offs-and-enterprise-alternatives](backend/README.md#neo4j-community-edition-trade-offs-and-enterprise-alternatives) |

## Boundaries not to cross
- Frontend components never call Neo4j directly; the Vue renderer only reads
  static `data/*.json`.
- Only the backend holds Neo4j credentials and the access policy.
- Import order matters: aircraft graph first, then the SDLC overlay (cross-domain
  edges resolve against existing aircraft node ids).
