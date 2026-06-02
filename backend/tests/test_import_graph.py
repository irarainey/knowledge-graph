"""Unit tests for the Neo4j knowledge-graph import script."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import import_graph
from import_graph import (
    DEFAULT_JSON_PATH,
    GraphImporter,
    Neo4jConfig,
    build_node_query,
    build_relationship_query,
    load_graph,
    sanitize_identifier,
)


# ── sanitize_identifier ──────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["Aircraft", "HAS_SYSTEM", "_private", "Node123"])
def test_sanitize_identifier_accepts_valid(name: str) -> None:
    assert sanitize_identifier(name, "label") == name


@pytest.mark.parametrize("name", ["123Bad", "has space", "back`tick", "drop;table", "", "kebab-case"])
def test_sanitize_identifier_rejects_invalid(name: str) -> None:
    with pytest.raises(ValueError, match="Unsafe label"):
        sanitize_identifier(name, "label")


# ── build_node_query ─────────────────────────────────────────────────────────
def test_build_node_query_single_label() -> None:
    query = build_node_query(["Aircraft"])
    assert query == "MERGE (n:`Aircraft` {id: $id})\nSET n += $props"


def test_build_node_query_multi_label_sets_extra_labels() -> None:
    query = build_node_query(["Aircraft", "LightAircraft"])
    assert "MERGE (n:`Aircraft` {id: $id})" in query
    assert query.endswith("SET n:`LightAircraft`")


def test_build_node_query_requires_labels() -> None:
    with pytest.raises(ValueError, match="at least one label"):
        build_node_query([])


def test_build_node_query_rejects_injection() -> None:
    with pytest.raises(ValueError, match="Unsafe label"):
        build_node_query(["Aircraft`) DETACH DELETE n //"])


# ── build_relationship_query ─────────────────────────────────────────────────
def test_build_relationship_query() -> None:
    query = build_relationship_query("HAS_SYSTEM")
    assert "MERGE (a)-[r:`HAS_SYSTEM`]->(b)" in query
    assert "MATCH (a {id: $start})" in query
    assert "MATCH (b {id: $end})" in query


def test_build_relationship_query_rejects_injection() -> None:
    with pytest.raises(ValueError, match="Unsafe relationship type"):
        build_relationship_query("FOO`]->() DELETE n //")


# ── load_graph ───────────────────────────────────────────────────────────────
def test_load_graph_reads_repo_data() -> None:
    graph = load_graph(DEFAULT_JSON_PATH)
    assert isinstance(graph["nodes"], list) and len(graph["nodes"]) > 0
    assert isinstance(graph["relationships"], list) and len(graph["relationships"]) > 0


def test_load_graph_rejects_non_graph(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"foo": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="missing 'nodes' key"):
        load_graph(bad)


# ── Neo4jConfig.from_env ─────────────────────────────────────────────────────
def test_config_from_env_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "password")
    monkeypatch.delenv("NEO4J_DATABASE", raising=False)
    config = Neo4jConfig.from_env()
    assert config.uri == "bolt://localhost:7687"
    assert config.database == "neo4j"  # default


def test_config_from_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit, match="Missing required environment variable"):
        Neo4jConfig.from_env()


# ── GraphImporter (fake driver) ──────────────────────────────────────────────
class FakeResult:
    def consume(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def run(self, query: str, **params: Any) -> FakeResult:
        self.calls.append((query, params))
        return FakeResult()


class FakeDriver:
    def __init__(self, session: FakeSession) -> None:
        self._session = session
        self.session_kwargs: dict[str, Any] = {}

    def session(self, **kwargs: Any) -> FakeSession:
        self.session_kwargs = kwargs
        return self._session


def test_importer_writes_nodes_and_relationships() -> None:
    session = FakeSession()
    driver = FakeDriver(session)
    graph = {
        "nodes": [
            {"id": "G-ECHO", "labels": ["Aircraft", "LightAircraft"], "properties": {"name": "G-ECHO", "mtow_kg": 1111}},
            {"id": "airframe", "labels": ["System"], "properties": {"name": "Airframe"}},
        ],
        "relationships": [
            {"type": "HAS_SYSTEM", "startNode": "G-ECHO", "endNode": "airframe", "properties": {}},
        ],
    }

    nodes, rels = GraphImporter(driver, "neo4j").import_graph(graph)  # type: ignore[arg-type]

    assert (nodes, rels) == (2, 1)
    assert driver.session_kwargs == {"database": "neo4j"}
    assert len(session.calls) == 3  # 2 nodes + 1 relationship

    node_query, node_params = session.calls[0]
    assert "MERGE (n:`Aircraft` {id: $id})" in node_query
    assert node_params == {"id": "G-ECHO", "props": {"name": "G-ECHO", "mtow_kg": 1111}}

    rel_query, rel_params = session.calls[2]
    assert "MERGE (a)-[r:`HAS_SYSTEM`]->(b)" in rel_query
    assert rel_params == {"start": "G-ECHO", "end": "airframe", "props": {}}


def test_importer_clear_deletes_first() -> None:
    session = FakeSession()
    driver = FakeDriver(session)
    graph = {"nodes": [{"id": "a", "labels": ["X"], "properties": {}}], "relationships": []}

    GraphImporter(driver, "neo4j").import_graph(graph, clear=True)  # type: ignore[arg-type]

    assert session.calls[0][0] == "MATCH (n) DETACH DELETE n"


def test_importer_real_data_roundtrip() -> None:
    session = FakeSession()
    driver = FakeDriver(session)
    graph = load_graph(DEFAULT_JSON_PATH)
    expected_nodes = len(graph["nodes"])
    expected_rels = len(graph["relationships"])

    nodes, rels = GraphImporter(driver, "neo4j").import_graph(graph)  # type: ignore[arg-type]

    assert (nodes, rels) == (expected_nodes, expected_rels)
    assert len(session.calls) == expected_nodes + expected_rels


def test_module_exposes_main() -> None:
    assert callable(import_graph.main)
