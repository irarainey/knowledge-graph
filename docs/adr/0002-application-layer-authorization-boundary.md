# ADR-0002: Authorization enforced entirely in the application layer

Date: 2026-06-09
Status: Accepted

## Context
The project runs on stock Neo4j **Community Edition**, which has no RBAC, no
label/relationship/property-level security, no multiple databases, and no
Fabric/composite databases. These are Enterprise-only features.

## Decision
All authorization — capability grants and row-level clearance — is enforced
in the FastAPI backend (policy store + structured-intent query builder +
injected clearance filter + post-retrieval redaction). Neo4j itself enforces
nothing.

## Rejected / foregone alternative
Native in-database access control (Neo4j Enterprise `GRANT`/`DENY` on
labels, relationship types, and properties) is documented as the Enterprise
alternative, not chosen for this PoC. This is a local-running PoC with no
Neo4j Enterprise licence — Community Edition was the only option in scope,
not a deliberate trade-off weighed against Enterprise. No formal
cost/benefit evaluation was performed.

## Consequences
- The backend is the *only* enforcement boundary — a bug in the query
  builder is a data leak (stated explicitly in backend/README.md).
- A `classification` property missing on a node defaults to visible to all,
  because Community has no property-existence constraint to guarantee it's
  always present.
