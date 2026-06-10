"""Backend-mediated, authorised access to externalised document content.

This is the **security boundary** for document bodies. The graph holds only metadata; the
body is fetched from a :class:`~documents.store.DocumentStore` and returned to the answer
model **only** after, in order:

1. **Authorization** — the same two dimensions as graph queries: the principal must be
   granted the ``Document`` entity *and* the ``document`` sensitivity category (capability),
   and the document's in-graph ``classification`` must be within the principal's clearance
   (row-level). A denial raises :class:`~authz.AuthorizationError`, exactly as a denied query
   does, so the agent relays a refusal.
2. **Integrity** — the fetched bytes are checksum-verified against the graph's recorded
   ``checksum`` (tamper / drift detection); a mismatch raises :class:`DocumentIntegrityError`.
3. **Sanitisation + excerpting** — the body is untrusted text (possible prompt injection), so
   it is wrapped as data and truncated to a character cap before the model ever sees it.

The opaque ``storageRef`` (and any blob URI) is **never** placed in the returned excerpt, so
it cannot leak to the LLM.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from authz import AuthorizationError, PolicyStore, Principal
from common.logging_config import get_logger
from documents.store import DocumentStore

logger = get_logger(__name__)

__all__ = [
    "DOCUMENT_CATEGORY",
    "DOCUMENT_ENTITY",
    "DocumentExcerpt",
    "DocumentIntegrityError",
    "DocumentMeta",
    "authorize_document",
    "load_document_excerpt",
    "match_document",
    "sanitize_document_text",
]

DOCUMENT_ENTITY = "Document"
# Sensitivity category that gates reading a document *body* (its metadata stays `basic`).
DOCUMENT_CATEGORY = "document"


class DocumentIntegrityError(RuntimeError):
    """Fetched content failed checksum verification against the graph's recorded value."""


class DocumentMeta(BaseModel):
    """Index metadata for one Document node (from the graph), used to fetch its body.

    ``storageRef`` is an opaque internal key into the document store and must never be
    surfaced to the LLM or the client.
    """

    documentId: str
    name: str = ""
    title: str = ""
    contentType: str = "text/plain"
    version: int | None = None
    classification: str | None = None
    storageRef: str
    checksum: str | None = None


class DocumentExcerpt(BaseModel):
    """An authorised, integrity-checked, truncated document body returned to the agent.

    Carries provenance for the debug panel / audit, but **no** ``storageRef`` or URI.
    """

    documentId: str
    title: str
    contentType: str
    version: int | None = None
    text: str
    truncated: bool = False
    charCount: int = Field(description="Length in characters of the returned (post-truncation) text.")


def match_document(reference: str, metas: list[DocumentMeta]) -> DocumentMeta | None:
    """Resolve a free-text document reference from the model to exactly one Document.

    Matching is deterministic and case-insensitive, tried most- to least-specific:
    exact ``documentId`` → exact ``title``/``name`` → unique substring of ``title``/``name``.
    Returns ``None`` if nothing matches or a substring is ambiguous (matches >1 document).
    """
    ref = reference.strip().casefold()
    if not ref:
        return None
    for meta in metas:
        if meta.documentId.casefold() == ref:
            return meta
    for meta in metas:
        if meta.title.casefold() == ref or meta.name.casefold() == ref:
            return meta
    substring_hits = [meta for meta in metas if ref in meta.title.casefold() or ref in meta.name.casefold()]
    if len(substring_hits) == 1:
        return substring_hits[0]
    return None


def authorize_document(principal: Principal, meta: DocumentMeta, policy: PolicyStore) -> None:
    """Enforce the same two-dimensional authorization as graph queries, for a document body.

    :raises AuthorizationError: if the entity or ``document`` category is not granted, or the
        document's classification is above the principal's clearance.
    """
    if DOCUMENT_ENTITY not in principal.entities:
        raise AuthorizationError("Documents are not available to this identity.")
    if DOCUMENT_CATEGORY not in principal.categories:
        raise AuthorizationError("Reading document content is not permitted for this identity.")
    if meta.classification is not None and meta.classification not in policy.allowed_classifications(principal):
        raise AuthorizationError("This document's classification is above your clearance.")


def sanitize_document_text(text: str, char_cap: int) -> tuple[str, bool]:
    """Make untrusted document text safe-ish to hand to the answer model, and cap its length.

    The body may contain prompt-injection ("ignore previous instructions…"); it is treated as
    **data**, never instructions (the tool result frames it as such and the system prompt is
    told document content is reference data). Here we normalise line endings, strip control
    characters, and truncate to ``char_cap``. Returns ``(text, truncated)``.
    """
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    # Drop control characters except tab/newline so nothing odd reaches the model.
    cleaned = "".join(ch for ch in normalised if ch == "\n" or ch == "\t" or ord(ch) >= 0x20)
    if len(cleaned) > char_cap:
        return cleaned[:char_cap].rstrip() + "\n…[content truncated]", True
    return cleaned, False


def _verify_checksum(meta: DocumentMeta, raw: bytes) -> None:
    if not meta.checksum:
        logger.warning("Document %s has no recorded checksum; skipping integrity check.", meta.documentId)
        return
    algo, _, expected = meta.checksum.partition(":")
    if algo.lower() != "sha256" or not expected:
        raise DocumentIntegrityError(f"Unsupported checksum format for {meta.documentId}: {meta.checksum!r}.")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected.lower():
        raise DocumentIntegrityError(f"Checksum mismatch for {meta.documentId}: expected {expected[:12]}…, got {actual[:12]}….")


def load_document_excerpt(
    reference: str,
    metas: list[DocumentMeta],
    principal: Principal,
    policy: PolicyStore,
    store: DocumentStore,
    *,
    char_cap: int,
) -> DocumentExcerpt:
    """Resolve, authorise, fetch, verify, sanitise and excerpt a document body.

    :raises AuthorizationError: if the principal may not read the matched document, or no
        document matched (the existence of a name is itself withheld behind the same refusal).
    :raises DocumentIntegrityError: if the fetched bytes fail checksum verification.
    """
    meta = match_document(reference, metas)
    if meta is None:
        # Don't disclose which documents exist beyond the principal's catalogue; a generic
        # refusal also covers ambiguous references.
        raise AuthorizationError(f"No accessible document matches {reference!r}.")
    authorize_document(principal, meta, policy)
    raw = store.fetch(meta.storageRef)
    _verify_checksum(meta, raw)
    text, truncated = sanitize_document_text(raw.decode("utf-8", errors="replace"), char_cap)
    logger.info(
        "Document content released: %s (v%s, %d chars%s) to %s",
        meta.documentId,
        meta.version,
        len(text),
        ", truncated" if truncated else "",
        principal.id,
    )
    return DocumentExcerpt(
        documentId=meta.documentId,
        title=meta.title or meta.name,
        contentType=meta.contentType,
        version=meta.version,
        text=text,
        truncated=truncated,
        charCount=len(text),
    )
