"""Tests for Area 4 — externalised document content.

These cover the two security boundaries for document bodies:

* the dumb content store (:mod:`documents.store`) — path-traversal safety and not-found
  behaviour, but never authorization; and
* the access service (:mod:`documents.access`) — the real gate: deterministic reference
  resolution, two-dimensional authorization (entity + ``document`` category), clearance on
  the document's classification, checksum integrity, and untrusted-text sanitisation.

The authorization assertions mirror the query-builder tests: a denial must be a refusal
(:class:`AuthorizationError`), and unauthorised content must never be returned — not merely
hidden after the fact.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from authz import AuthorizationError, PolicyStore, Principal
from documents.access import (
    DocumentExcerpt,
    DocumentIntegrityError,
    DocumentMeta,
    authorize_document,
    load_document_excerpt,
    match_document,
    sanitize_document_text,
)
from documents.store import (
    DocumentNotFoundError,
    DocumentStore,
    DocumentStoreError,
    LocalFileDocumentStore,
)

STORE = PolicyStore.load()


def principal(user: str) -> Principal:
    return STORE.resolve_principal(user)


PUBLIC = principal("public")
MAINTENANCE = principal("maintenance_engineer")
OPS = principal("restricted_ops")


def _meta(
    *,
    document_id: str = "DOC-0001",
    title: str = "Pilot's Operating Handbook",
    storage_ref: str = "poh",
    classification: str | None = None,
    checksum: str | None = None,
) -> DocumentMeta:
    return DocumentMeta(
        documentId=document_id,
        title=title,
        storageRef=storage_ref,
        classification=classification,
        checksum=checksum,
        version=1,
    )


class _StubStore(DocumentStore):
    """An in-memory store mapping storageRef -> bytes, for access-service tests."""

    def __init__(self, content: dict[str, bytes]) -> None:
        self._content = content

    def fetch(self, storage_ref: str) -> bytes:
        try:
            return self._content[storage_ref]
        except KeyError as exc:
            raise DocumentNotFoundError(storage_ref) from exc


# --------------------------------------------------------------------------- store


def test_local_store_fetches_by_storage_ref(tmp_path: Path) -> None:
    (tmp_path / "poh.md").write_bytes(b"never-exceed speed is 163 KIAS")
    store = LocalFileDocumentStore(tmp_path)
    assert store.fetch("poh") == b"never-exceed speed is 163 KIAS"


def test_local_store_missing_ref_raises_not_found(tmp_path: Path) -> None:
    store = LocalFileDocumentStore(tmp_path)
    with pytest.raises(DocumentNotFoundError):
        store.fetch("does-not-exist")


@pytest.mark.parametrize("evil", ["../secret", "a/b", "a\\b", "..", ".", "\x00", ""])
def test_local_store_rejects_path_traversal(tmp_path: Path, evil: str) -> None:
    (tmp_path.parent / "secret.md").write_bytes(b"top secret")
    store = LocalFileDocumentStore(tmp_path)
    with pytest.raises(DocumentStoreError):
        store.fetch(evil)


def test_seeded_documents_match_recorded_checksums() -> None:
    """The bundled content files agree with the checksums stored on the graph nodes."""
    import json

    graph = json.loads((Path(__file__).resolve().parents[2] / "data" / "knowledge-graph.json").read_text())
    docs_dir = Path(__file__).resolve().parents[2] / "data" / "documents"
    documents = [n["properties"] for n in graph["nodes"] if "Document" in n.get("labels", [])]
    assert documents, "expected Document nodes in the seed graph"
    for props in documents:
        raw = (docs_dir / f"{props['storageRef']}.md").read_bytes()
        actual = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        assert actual == props["checksum"], f"checksum drift for {props['documentId']}"


# ------------------------------------------------------------------- match_document


def test_match_document_by_exact_id() -> None:
    metas = [_meta(document_id="DOC-0001"), _meta(document_id="DOC-0002", title="Maintenance Manual", storage_ref="maint")]
    hit = match_document("doc-0002", metas)
    assert hit is not None and hit.documentId == "DOC-0002"


def test_match_document_by_unique_substring() -> None:
    metas = [_meta(title="Pilot's Operating Handbook"), _meta(document_id="DOC-0002", title="Maintenance Manual", storage_ref="maint")]
    hit = match_document("operating", metas)
    assert hit is not None and hit.documentId == "DOC-0001"


def test_match_document_ambiguous_substring_returns_none() -> None:
    metas = [
        _meta(document_id="DOC-0001", title="Engine Manual A", storage_ref="a"),
        _meta(document_id="DOC-0002", title="Engine Manual B", storage_ref="b"),
    ]
    assert match_document("manual", metas) is None


def test_match_document_no_match_returns_none() -> None:
    assert match_document("nonexistent", [_meta()]) is None
    assert match_document("   ", [_meta()]) is None


# --------------------------------------------------------------- authorize_document


def test_authorize_document_denies_public_at_entity_gate() -> None:
    with pytest.raises(AuthorizationError):
        authorize_document(PUBLIC, _meta(), STORE)


def test_authorize_document_allows_maintenance() -> None:
    # Should not raise.
    authorize_document(MAINTENANCE, _meta(), STORE)


def test_authorize_document_denies_classification_above_clearance() -> None:
    secret = _meta(classification="secret")
    with pytest.raises(AuthorizationError):
        authorize_document(MAINTENANCE, secret, STORE)


def test_authorize_document_allows_classification_within_clearance() -> None:
    cleared = STORE.allowed_classifications(OPS)[0]
    authorize_document(OPS, _meta(classification=cleared), STORE)


# --------------------------------------------------------------- sanitize_document_text


def test_sanitize_strips_control_chars_and_normalises_newlines() -> None:
    text, truncated = sanitize_document_text("a\r\nb\x07c\td", char_cap=100)
    assert text == "a\nbc\td"
    assert truncated is False


def test_sanitize_truncates_to_cap() -> None:
    text, truncated = sanitize_document_text("x" * 50, char_cap=10)
    assert truncated is True
    assert text.startswith("x" * 10)
    assert "content truncated" in text


# ----------------------------------------------------------------- load_document_excerpt


def test_load_excerpt_happy_path_verifies_checksum() -> None:
    body = b"V_NE is 163 KIAS."
    checksum = f"sha256:{hashlib.sha256(body).hexdigest()}"
    metas = [_meta(checksum=checksum)]
    store = _StubStore({"poh": body})

    excerpt = load_document_excerpt("DOC-0001", metas, MAINTENANCE, STORE, store, char_cap=8000)

    assert isinstance(excerpt, DocumentExcerpt)
    assert excerpt.documentId == "DOC-0001"
    assert "163 KIAS" in excerpt.text
    # storageRef must never appear in the returned excerpt.
    assert "poh" not in excerpt.model_dump_json() or "storageRef" not in excerpt.model_dump()


def test_load_excerpt_checksum_mismatch_raises_integrity_error() -> None:
    metas = [_meta(checksum="sha256:" + "0" * 64)]
    store = _StubStore({"poh": b"tampered"})
    with pytest.raises(DocumentIntegrityError):
        load_document_excerpt("DOC-0001", metas, MAINTENANCE, STORE, store, char_cap=8000)


def test_load_excerpt_denied_for_public() -> None:
    body = b"anything"
    metas = [_meta(checksum=f"sha256:{hashlib.sha256(body).hexdigest()}")]
    store = _StubStore({"poh": body})
    with pytest.raises(AuthorizationError):
        load_document_excerpt("DOC-0001", metas, PUBLIC, STORE, store, char_cap=8000)


def test_load_excerpt_unmatched_reference_is_refusal_not_disclosure() -> None:
    metas = [_meta()]
    store = _StubStore({"poh": b"x"})
    with pytest.raises(AuthorizationError):
        load_document_excerpt("nonexistent document", metas, MAINTENANCE, STORE, store, char_cap=8000)
