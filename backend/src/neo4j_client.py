"""Async Neo4j connectivity for the query API.

Connection settings are read from the environment (optionally via a ``.env``
file). The client wraps a single shared :class:`~neo4j.AsyncDriver` and exposes a
small ``run_query`` helper that returns plain, JSON-serialisable records.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, LiteralString, cast

from dotenv import load_dotenv
from neo4j import AsyncGraphDatabase
from neo4j.graph import Node, Relationship
from neo4j.graph import Path as GraphPath

# JSON-native scalar types that need no conversion.
_JSON_SCALARS = (str, bool, int, float)

# Schema-introspection queries (read-only, APOC-free). Cheap on a small PoC graph.
SCHEMA_NODE_SAMPLES = "MATCH (n) RETURN labels(n) AS labels, properties(n) AS props"
SCHEMA_RELATIONSHIPS = "MATCH (a)-[r]->(b) RETURN DISTINCT labels(a) AS startLabels, type(r) AS type, labels(b) AS endLabels ORDER BY type"
SCHEMA_RELATIONSHIP_PROPERTIES = (
    "MATCH ()-[r]->() WITH type(r) AS type, keys(r) AS ks UNWIND ks AS k RETURN type, collect(DISTINCT k) AS properties ORDER BY type"
)


def load_env(env_file: Path | None = None) -> None:
    """Load Neo4j settings from a ``.env`` file.

    With no argument, loads ``backend/.env`` (next to ``pyproject.toml``) and then
    any ``.env`` found in the current working directory.
    """
    if env_file is not None:
        load_dotenv(env_file)
        return
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    load_dotenv()


@dataclass(frozen=True)
class Neo4jSettings:
    uri: str
    username: str
    password: str
    database: str

    @classmethod
    def from_env(cls) -> Neo4jSettings:
        missing = [v for v in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD") if not os.environ.get(v)]
        if missing:
            raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")
        return cls(
            uri=os.environ["NEO4J_URI"],
            username=os.environ["NEO4J_USERNAME"],
            password=os.environ["NEO4J_PASSWORD"],
            database=os.environ.get("NEO4J_DATABASE", "neo4j"),
        )


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


class Neo4jClient:
    """Thin async wrapper around the Neo4j driver for running ad-hoc queries."""

    def __init__(self, settings: Neo4jSettings) -> None:
        self._settings = settings
        self._driver = AsyncGraphDatabase.driver(settings.uri, auth=(settings.username, settings.password))

    async def verify_connectivity(self) -> None:
        await self._driver.verify_connectivity()

    async def close(self) -> None:
        await self._driver.close()

    async def run_query(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        database: str | None = None,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Execute a Cypher query and return ``(columns, records)``.

        ``records`` are JSON-serialisable dicts keyed by the query's return names.
        """
        db = database or self._settings.database
        async with self._driver.session(database=db) as session:
            result = await session.run(cast(LiteralString, query), parameters or {})
            rows = [record async for record in result]
            columns = list(result.keys())
        records = [{key: to_jsonable(value) for key, value in row.items()} for row in rows]
        return columns, records
