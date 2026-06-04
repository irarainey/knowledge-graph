"""Async Neo4j connectivity for the query API.

Connection settings are read from the environment (optionally via a ``.env``
file). The client wraps a single shared :class:`~neo4j.AsyncDriver` and exposes a
small ``run_query`` helper that returns plain, JSON-serialisable records.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, LiteralString, cast

from neo4j import AsyncGraphDatabase

from common.serialization import to_jsonable


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
