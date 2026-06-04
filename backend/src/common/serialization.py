"""Convert Neo4j driver values into JSON-serialisable data.

Reusable across the async client, schema introspection and retriever helpers: it
turns nodes, relationships, paths and temporal/spatial values into plain JSON
types so results can be returned over HTTP or fed to an LLM as context.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from neo4j.graph import Node, Relationship
from neo4j.graph import Path as GraphPath

# JSON-native scalar types that need no conversion.
_JSON_SCALARS = (str, bool, int, float)


def to_jsonable(value: Any) -> Any:
    """Recursively convert a Neo4j record value into JSON-serialisable data.

    Scalars and nested lists/dicts pass through; nodes and relationships become
    their property maps (with ``_labels``/``_type`` markers); temporal, spatial
    and other driver-specific objects fall back to their string representation.
    """
    if value is None or isinstance(value, _JSON_SCALARS):
        return value
    if isinstance(value, (dt.date, dt.time, dt.datetime)):
        return value.isoformat()
    if isinstance(value, Node):
        return {"_labels": sorted(value.labels), **{str(key): to_jsonable(item) for key, item in value.items()}}
    if isinstance(value, Relationship):
        return {"_type": value.type, **{str(key): to_jsonable(item) for key, item in value.items()}}
    if isinstance(value, GraphPath):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [to_jsonable(item) for item in value]
    return str(value)
