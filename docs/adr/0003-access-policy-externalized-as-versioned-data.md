# ADR-0003: Access policy externalized as versioned JSON data

Date: 2026-06-09
Status: Accepted

## Context
Who may see what needs to change independently of the graph data and the
ontology, and every answer needs to be attributable to a specific policy
version for audit.

## Decision
The access policy lives in `backend/policy/access-policy.json` (path
configurable via `ACCESS_POLICY_PATH`), loaded and validated at startup.
An invalid or missing policy fails the service closed. Every answer records
the `policyVersion` it was resolved under.

## Rejected alternatives
Not documented. The repository states the benefit (independent versioning,
auditability) but not what alternative representation (e.g. hardcoded in
source, a database table) was considered and rejected. This is a local PoC
for testing ideas — a JSON file was simply the simplest workable option;
no formal evaluation of alternatives (config service, database-backed
policy, etc.) was carried out.

## Consequences
- Policy changes ship without a graph re-import.
- Startup fails hard on an invalid policy file (no silent fallback).
