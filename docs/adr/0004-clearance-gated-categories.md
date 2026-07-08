# ADR-0004: Clearance-gated categories (opt-in field-level redaction)

Date: 2026-06-10
Status: Accepted

## Context
The default row-level clearance model hides a whole classified row from an
identity not cleared for it — including from aggregates. This means an
identity like `maintenance_engineer` could not get a total flying-hours
figure across *all* flights (including classified ones) without also being
granted full route visibility on those flights.

## Decision
A granted category can be marked `clearanceGatedCategories` for an identity.
For those categories, classified rows stay visible but the category's
fields are nulled field-by-field (a `CASE WHEN` on row clearance); other
fields, plain counts, and non-gated aggregates still include the row.
Off by default; granted per-identity only where needed.

## Rejected alternative / trade-off acknowledged
The alternative is the default whole-row hide. The repository is explicit
that clearance-gating is a deliberate relaxation that **reopens an
existence/inference channel** for gated rows — accepted as a controlled,
auditable trade-off, not a lower bar generally.

## Consequences
- `maintenance_engineer` can total flying hours across all flights but
  never learn where a classified flight went.
- Any new clearance-gated category must be justified per-identity by
  need-to-know (documented expectation, not a mechanical check).
