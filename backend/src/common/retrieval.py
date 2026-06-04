"""Helpers for neo4j-graphrag retriever results.

Reusable across any agent using a neo4j-graphrag retriever: format Neo4j records
into retriever items (keeping the structured row alongside the JSON context), and
pull the generated Cypher and rows back out of a retriever result.
"""

from __future__ import annotations

import json
from typing import Any

from neo4j_graphrag.types import RetrieverResult, RetrieverResultItem

from neo4j_client import to_jsonable


def record_to_item(record: Any) -> RetrieverResultItem:
    """Format a Neo4j record into a retriever item, keeping the row JSON-serialisable.

    The JSON ``content`` becomes the LLM context; the structured dict is stashed in
    ``metadata`` so the API can return the raw rows it answered from.
    """
    data = to_jsonable(dict(record))
    return RetrieverResultItem(content=json.dumps(data), metadata={"record": data})


def extract_cypher_and_records(retriever_result: RetrieverResult | None) -> tuple[list[str], list[dict[str, Any]]]:
    """Pull the generated Cypher and the JSON-serialisable rows out of a retriever result."""
    metadata = (retriever_result.metadata or {}) if retriever_result else {}
    cypher = metadata.get("cypher")
    items = retriever_result.items if retriever_result else []
    records = [item.metadata["record"] for item in items if item.metadata and "record" in item.metadata]
    return ([cypher] if cypher else [], records)
