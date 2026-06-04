"""Import a Neo4j/APOC-style knowledge-graph JSON export into a running Neo4j.

Connection settings are read from the environment (optionally via a ``.env``
file). Every node and relationship in the JSON file is upserted, so re-running is
idempotent: nodes are matched on their ``id`` property and relationships on the
(start node, type, end node) triple.

Usage:
    uv run poe import-graph                 # import data/knowledge-graph.json
    uv run python scripts/import_graph.py --clear
    uv run python scripts/import_graph.py --file path/to/graph.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, LiteralString, cast

from neo4j import Driver, GraphDatabase, Session

# This script lives in backend/scripts, outside the ``src`` package. Add ``src`` to
# the path so the shared ``common`` helpers (env loading) import when the script is
# run directly (the poe task and tests put it on the path too).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common.env import load_env

# Repo layout: <repo>/backend/scripts/import_graph.py -> <repo>/data/knowledge-graph.json
DEFAULT_JSON_PATH = Path(__file__).resolve().parents[2] / "data" / "knowledge-graph.json"

# Cypher cannot parametrise labels or relationship types, so they are interpolated
# into the query string. Restricting them to a strict identifier pattern prevents
# Cypher injection via crafted names in the source JSON.
_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sanitize_identifier(name: str, kind: str) -> str:
    """Validate a label or relationship-type name for safe Cypher interpolation."""
    if not isinstance(name, str) or not _VALID_IDENTIFIER.match(name):
        raise ValueError(f"Unsafe {kind} name in graph data: {name!r}")
    return name


def build_node_query(labels: list[str]) -> str:
    """Build a MERGE upsert query for a node with the given labels.

    The node is matched/created on its primary label plus ``id``; any additional
    labels and all properties are then set.
    """
    if not labels:
        raise ValueError("Every node must have at least one label")
    primary = sanitize_identifier(labels[0], "label")
    extra = "".join(f":`{sanitize_identifier(label, 'label')}`" for label in labels[1:])
    query = f"MERGE (n:`{primary}` {{id: $id}})\nSET n += $props"
    if extra:
        query += f"\nSET n{extra}"
    return query


def build_relationship_query(rel_type: str) -> str:
    """Build a MERGE upsert query for a relationship of the given type."""
    safe = sanitize_identifier(rel_type, "relationship type")
    return f"MATCH (a {{id: $start}})\nMATCH (b {{id: $end}})\nMERGE (a)-[r:`{safe}`]->(b)\nSET r += $props"


def load_graph(path: Path) -> dict[str, Any]:
    """Load and minimally validate a graph JSON export."""
    with path.open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    if "nodes" not in data:
        raise ValueError(f"{path} does not look like a graph export (missing 'nodes' key)")
    return data


@dataclass
class Neo4jConfig:
    # Deliberate near-duplicate of neo4j_client.Neo4jSettings (same env vars/fields).
    # Kept separate because this CLI raises SystemExit on missing config (clean exit
    # with a message), whereas the API's Neo4jSettings raises RuntimeError so the
    # FastAPI lifespan can catch it and run with the /ask endpoint disabled. Merging
    # the two would conflate those two error-handling contracts.
    uri: str
    user: str
    password: str
    database: str

    @classmethod
    def from_env(cls) -> Neo4jConfig:
        missing = [v for v in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD") if not os.environ.get(v)]
        if missing:
            raise SystemExit(f"Missing required environment variable(s): {', '.join(missing)}")
        return cls(
            uri=os.environ["NEO4J_URI"],
            user=os.environ["NEO4J_USERNAME"],
            password=os.environ["NEO4J_PASSWORD"],
            database=os.environ.get("NEO4J_DATABASE", "neo4j"),
        )


class GraphImporter:
    """Upserts a parsed graph export into Neo4j using its driver."""

    def __init__(self, driver: Driver, database: str) -> None:
        self._driver = driver
        self._database = database

    def import_graph(self, graph: dict[str, Any], *, clear: bool = False) -> tuple[int, int]:
        nodes = graph.get("nodes", [])
        relationships = graph.get("relationships", [])
        with self._driver.session(database=self._database) as session:
            if clear:
                print("Clearing existing graph...")
                session.run("MATCH (n) DETACH DELETE n").consume()
            for node in nodes:
                self._write_node(session, node)
            for rel in relationships:
                self._write_relationship(session, rel)
        return len(nodes), len(relationships)

    @staticmethod
    def _write_node(session: Session, node: dict[str, Any]) -> None:
        query = build_node_query(node.get("labels", []))
        session.run(cast(LiteralString, query), id=node["id"], props=dict(node.get("properties", {}))).consume()

    @staticmethod
    def _write_relationship(session: Session, rel: dict[str, Any]) -> None:
        query = build_relationship_query(rel["type"])
        params = {"start": rel["startNode"], "end": rel["endNode"], "props": dict(rel.get("properties", {}))}
        session.run(cast(LiteralString, query), **params).consume()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import knowledge-graph JSON into Neo4j.")
    parser.add_argument("--file", type=Path, default=DEFAULT_JSON_PATH, help="Path to the graph JSON export")
    parser.add_argument("--clear", action="store_true", help="Delete all existing data before importing")
    parser.add_argument("--env-file", type=Path, default=None, help="Path to a .env file with Neo4j settings")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env(args.env_file)

    config = Neo4jConfig.from_env()
    graph = load_graph(args.file)

    print(f"Connecting to Neo4j at {config.uri} (database: {config.database})...")
    driver = GraphDatabase.driver(config.uri, auth=(config.user, config.password))
    try:
        driver.verify_connectivity()
        node_count, rel_count = GraphImporter(driver, config.database).import_graph(graph, clear=args.clear)
    finally:
        driver.close()

    print(f"Imported {node_count} nodes and {rel_count} relationships from {args.file}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
