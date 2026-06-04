"""Unit tests for the Neo4j query API and client helpers.

The Neo4j driver is faked so these tests run without a live database. The FastAPI
app's lifespan is overridden to inject the fake client.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from neo4j.exceptions import Neo4jError

import app
from neo4j_client import Neo4jSettings, to_jsonable


# ── to_jsonable ──────────────────────────────────────────────────────────────
def test_to_jsonable_passes_through_scalars() -> None:
    assert to_jsonable(1) == 1
    assert to_jsonable(1.5) == 1.5
    assert to_jsonable("x") == "x"
    assert to_jsonable(True) is True
    assert to_jsonable(None) is None


def test_to_jsonable_converts_temporal_to_isoformat() -> None:
    assert to_jsonable(dt.date(2026, 5, 25)) == "2026-05-25"
    assert to_jsonable(dt.datetime(2026, 5, 25, 12, 30)) == "2026-05-25T12:30:00"


def test_to_jsonable_recurses_into_collections() -> None:
    value = {"a": [1, dt.date(2026, 1, 1)], "b": {"c": 2}}
    assert to_jsonable(value) == {"a": [1, "2026-01-01"], "b": {"c": 2}}


def test_to_jsonable_stringifies_unknown_objects() -> None:
    class Custom:
        def __str__(self) -> str:
            return "custom-repr"

    assert to_jsonable(Custom()) == "custom-repr"


# ── Neo4jSettings.from_env ───────────────────────────────────────────────────
def test_settings_from_env_reads_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.setenv("NEO4J_DATABASE", "graph")
    settings = Neo4jSettings.from_env()
    assert settings == Neo4jSettings("bolt://localhost:7687", "neo4j", "secret", "graph")


def test_settings_from_env_defaults_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.delenv("NEO4J_DATABASE", raising=False)
    assert Neo4jSettings.from_env().database == "neo4j"


def test_settings_from_env_raises_on_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="Missing required environment variable"):
        Neo4jSettings.from_env()


# ── /query endpoint ──────────────────────────────────────────────────────────
class FakeClient:
    """Stand-in for Neo4jClient that records calls and returns canned results."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.columns: list[str] = ["id"]
        self.records: list[dict[str, Any]] = [{"id": "io360"}]
        self.error: Exception | None = None

    async def run_query(
        self, query: str, parameters: dict[str, Any] | None = None, database: str | None = None
    ) -> tuple[list[str], list[dict[str, Any]]]:
        self.calls.append((query, parameters or {}, database))
        if self.error is not None:
            raise self.error
        return self.columns, self.records


@asynccontextmanager
async def _make_client(client: FakeClient) -> AsyncIterator[AsyncClient]:
    """Yield an HTTP client wired to the real app with the Neo4j client overridden."""
    app.app.dependency_overrides[app._get_client] = lambda: client
    transport = ASGITransport(app=app.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
    finally:
        app.app.dependency_overrides.clear()


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


async def test_query_returns_records(fake_client: FakeClient) -> None:
    async with _make_client(fake_client) as client:
        response = await client.post("/query", json={"query": "MATCH (n) RETURN n.id AS id"})
    assert response.status_code == 200
    body = response.json()
    assert body == {"columns": ["id"], "records": [{"id": "io360"}], "count": 1}


async def test_query_forwards_parameters_and_database(fake_client: FakeClient) -> None:
    async with _make_client(fake_client) as client:
        await client.post(
            "/query",
            json={"query": "RETURN $x AS x", "parameters": {"x": 1}, "database": "graph"},
        )
    assert fake_client.calls == [("RETURN $x AS x", {"x": 1}, "graph")]


async def test_query_rejects_empty_query(fake_client: FakeClient) -> None:
    async with _make_client(fake_client) as client:
        response = await client.post("/query", json={"query": ""})
    assert response.status_code == 422


async def test_query_maps_neo4j_error_to_400(fake_client: FakeClient) -> None:
    error = Neo4jError("Invalid syntax")
    error._message = "Invalid syntax"
    fake_client.error = error
    async with _make_client(fake_client) as client:
        response = await client.post("/query", json={"query": "NOT CYPHER"})
    assert response.status_code == 400
    assert "Invalid syntax" in response.json()["detail"]


async def test_health_endpoint(fake_client: FakeClient) -> None:
    async with _make_client(fake_client) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
