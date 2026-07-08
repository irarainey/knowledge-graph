# ADR-0007: Selected-user identity instead of authentication

Date: 2026-06-09
Status: Accepted (PoC scope)

## Context
The PoC needs to demonstrate identity-based authorization without building
a real authentication system.

## Decision
`/ask` accepts a `user` field naming one of a fixed set of identities
(a UI dropdown in the Streamlit chat). The backend resolves it server-side
via `PolicyStore.resolve_principal`; an unknown or omitted `user` resolves
to the least-privilege `defaultIdentity` (`public`), not to broad access.

## Rejected / deferred alternative
A verified token (e.g. an OIDC claim) carrying the identity, as would be
used in a real deployment. The repository states the enforcement mechanics
described elsewhere would be unchanged if this were swapped in — it is
explicitly framed as future work, not a rejected design. This is a local
PoC for testing ideas; building real authentication was out of scope rather
than actively rejected.

## Consequences
- The client is trusted only for *which* identity id it names, never for
  claims about that identity's rights.
- Conversation state must reset on identity switch (memory-leak prevention,
  called out in the plan's Phase-0 foundations).
