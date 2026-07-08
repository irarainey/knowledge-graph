# DECISIONS.md

Newest first. Full reasoning and rejected alternatives in `docs/adr/`.

- 2026-06-17 — SDLC/engineering overlay joined to the operational graph via
  cross-domain edges. *(feature addition, not a binding rule — no ADR)*
- 2026-06-10 — Document bodies stored outside the graph, fetched via a
  backend-mediated, checksum-verified path. → [ADR-0006](docs/adr/0006-externalized-document-storage.md)
- 2026-06-10 — Clearance-gated categories: opt-in field-level redaction instead
  of whole-row hiding, for identities that need aggregate visibility across
  classified rows. → [ADR-0004](docs/adr/0004-clearance-gated-categories.md)
- 2026-06-09 — Access policy externalized as versioned JSON data, loaded and
  validated at startup, fails closed. → [ADR-0003](docs/adr/0003-access-policy-externalized-as-versioned-data.md)
- 2026-06-09 — Authorization enforced entirely in the application layer, not
  the database (Neo4j Community Edition has no RBAC). → [ADR-0002](docs/adr/0002-application-layer-authorization-boundary.md)
- 2026-06-09 — Structured query-intent + backend-built Cypher replaces
  free-form LLM-generated Cypher. → [ADR-0001](docs/adr/0001-structured-intent-query-builder.md)
- 2026-06-09 — Identity is UI-selected, not authenticated, for the PoC.
  → [ADR-0007](docs/adr/0007-selected-identity-not-authentication.md)
- [pre-2026-06-10, exact date not isolated in git history] — Versioning kept
  narrow (valid-time + event-time on select entities only), not full
  bitemporal. → [ADR-0005](docs/adr/0005-narrow-versioning-scope.md)
