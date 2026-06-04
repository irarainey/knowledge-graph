"""Pydantic request/response models for the knowledge-graph API.

Re-exports the public models so callers can
``from models import QueryRequest, AskResponse``.
"""

from __future__ import annotations

from models.ask import AskRequest, AskResponse
from models.query import QueryRequest, QueryResponse

__all__ = ["AskRequest", "AskResponse", "QueryRequest", "QueryResponse"]
