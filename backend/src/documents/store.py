"""External document content store (Area 4 — scalability via externalised storage).

Large document **bodies** are kept out of the graph: the graph holds only metadata (the
index), and the body is fetched on demand from a pluggable :class:`DocumentStore`, keyed by
each Document node's opaque ``storageRef`` (never a URL). The default
:class:`LocalFileDocumentStore` reads content files from a directory; an
:class:`AzureBlobDocumentStore` stub documents the production shape.

The store is intentionally dumb: it only fetches bytes by opaque key. **Authorization,
checksum/version verification, sanitisation and excerpting live in the access service**
(:mod:`documents.access`) so the store can never be a policy-bypass path.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

from common.config import (
    DOCUMENT_STORE_PATH_DEFAULT,
    ENV_DOCUMENT_STORE_PATH,
)
from common.logging_config import get_logger

logger = get_logger(__name__)

__all__ = [
    "AzureBlobDocumentStore",
    "DocumentNotFoundError",
    "DocumentStore",
    "DocumentStoreError",
    "LocalFileDocumentStore",
    "build_document_store",
]


class DocumentStoreError(RuntimeError):
    """A document store could not return content (misconfiguration or backend failure)."""


class DocumentNotFoundError(DocumentStoreError):
    """No content exists for the requested ``storageRef``."""


class DocumentStore(ABC):
    """A content-addressable store of document bodies, keyed by opaque ``storageRef``.

    Implementations resolve an opaque internal key (never exposed to the LLM) to raw bytes.
    They perform **no** authorization — that is the access service's job.
    """

    @abstractmethod
    def fetch(self, storage_ref: str) -> bytes:
        """Return the raw bytes stored under ``storage_ref``.

        :raises DocumentNotFoundError: if no content exists for the key.
        :raises DocumentStoreError: on a backend/configuration failure.
        """


class LocalFileDocumentStore(DocumentStore):
    """A :class:`DocumentStore` backed by files in a local directory (the PoC default).

    Each ``storageRef`` maps to ``<root>/<storageRef>.md``. The key is validated to a single
    path segment so it can never traverse outside ``root`` (``..``/separators are rejected).
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def fetch(self, storage_ref: str) -> bytes:
        if not storage_ref or "/" in storage_ref or "\\" in storage_ref or storage_ref in (".", "..") or "\x00" in storage_ref:
            raise DocumentStoreError(f"Invalid storage reference {storage_ref!r}.")
        path = (self._root / f"{storage_ref}.md").resolve()
        # Defence in depth: confirm the resolved path is still inside the root.
        if self._root not in path.parents:
            raise DocumentStoreError(f"Resolved path for {storage_ref!r} escapes the store root.")
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise DocumentNotFoundError(f"No content for storage reference {storage_ref!r}.") from exc
        except OSError as exc:
            raise DocumentStoreError(f"Failed to read content for {storage_ref!r}: {exc}.") from exc


class AzureBlobDocumentStore(DocumentStore):
    """Production stub: fetch document bodies from Azure Blob Storage.

    Not used by the PoC (the local store is the default). It documents the intended
    production shape: a container of blobs keyed by ``storageRef``, accessed with a
    **managed identity** (or a short-lived, server-minted SAS) — never a public URL, and the
    blob URI is never surfaced to the LLM. Enable by installing ``azure-storage-blob`` and
    wiring credentials, then selecting this store in :func:`build_document_store`.
    """

    def __init__(self, account_url: str, container: str) -> None:
        self._account_url = account_url
        self._container = container

    def fetch(self, storage_ref: str) -> bytes:  # pragma: no cover - documented stub
        raise DocumentStoreError(
            "AzureBlobDocumentStore is a documented production stub and is not enabled in the PoC. "
            "Install azure-storage-blob, authenticate with a managed identity, and fetch the blob "
            f"keyed by {storage_ref!r} from container {self._container!r} at {self._account_url!r}."
        )


def build_document_store() -> DocumentStore:
    """Build the configured document store (local filesystem by default).

    The directory comes from ``DOCUMENT_STORE_PATH`` (relative paths resolve against the
    backend working directory). Selecting the Azure backend is a deliberate production step.
    """
    root = Path(os.getenv(ENV_DOCUMENT_STORE_PATH, DOCUMENT_STORE_PATH_DEFAULT))
    logger.debug("Building local document store rooted at %s", root.resolve())
    return LocalFileDocumentStore(root)
