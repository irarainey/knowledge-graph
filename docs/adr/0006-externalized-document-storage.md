# ADR-0006: Document bodies externalized, fetched via backend-mediated access

Date: 2026-06-10
Status: Accepted

## Context
Document nodes needed to reference large bodies of content (manuals, ADs,
checklists) without storing that content as graph properties, and without
letting the LLM bypass authorization to reach it.

## Decision
Each `Document` node keeps only metadata (`documentId`, `title`,
`contentType`, `version`, optional `classification`, an opaque `storageRef`,
a `sha256` checksum). The body lives in a pluggable `DocumentStore`
(local-filesystem default; documented Azure Blob stub, not enabled).
Access is backend-mediated: resolve reference → authorize (entity + document
category + clearance) → fetch → verify checksum → sanitize/excerpt. Reachable
only through the `/ask` agent's `fetch_document_content` typed tool — no
bypass endpoint. Logged to a separate `kg.audit.document` trail.

## Rejected alternative
Exposing blob URIs/storage references directly to the LLM. The repository
states this explicitly as rejected: bypass risk, names leak facts by
existence, and SAS URLs would appear in logs/conversation history.

## Consequences
- `storageRef` must never appear in the LLM context, the metadata event, or
  the audit log (only `documentId`/`version`/char-count do).
- Externalizing to a real blob store (Area 4's stated future step) expands
  the security boundary to include the blob's own access policy, which must
  match or exceed the pointer's policy.
