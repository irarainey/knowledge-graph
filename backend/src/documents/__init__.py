"""Externalised document content: storage abstraction + authorised access (Area 4)."""

from __future__ import annotations

from documents.access import (
    DOCUMENT_CATEGORY,
    DOCUMENT_ENTITY,
    DocumentExcerpt,
    DocumentIntegrityError,
    DocumentMeta,
    authorize_document,
    load_document_excerpt,
    match_document,
    sanitize_document_text,
)
from documents.store import (
    AzureBlobDocumentStore,
    DocumentNotFoundError,
    DocumentStore,
    DocumentStoreError,
    LocalFileDocumentStore,
    build_document_store,
)

__all__ = [
    "DOCUMENT_CATEGORY",
    "DOCUMENT_ENTITY",
    "AzureBlobDocumentStore",
    "DocumentExcerpt",
    "DocumentIntegrityError",
    "DocumentMeta",
    "DocumentNotFoundError",
    "DocumentStore",
    "DocumentStoreError",
    "LocalFileDocumentStore",
    "authorize_document",
    "build_document_store",
    "load_document_excerpt",
    "match_document",
    "sanitize_document_text",
]
