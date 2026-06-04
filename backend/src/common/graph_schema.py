"""Neo4j graph-schema introspection rendered as LLM prompt text.

Reusable across any agent that writes Cypher against a Neo4j graph: it reads the
live schema with plain Cypher (no APOC, so it works on a stock Community
container) and renders it with inferred property types and example values.
"""

from __future__ import annotations

import json
from typing import Any, LiteralString, cast

from neo4j import Driver, RoutingControl

from common.serialization import to_jsonable

# Schema-introspection queries (read-only, APOC-free). Cheap on a small PoC graph.
SCHEMA_NODE_SAMPLES = "MATCH (n) RETURN labels(n) AS labels, properties(n) AS props"
SCHEMA_RELATIONSHIPS = "MATCH (a)-[r]->(b) RETURN DISTINCT labels(a) AS startLabels, type(r) AS type, labels(b) AS endLabels ORDER BY type"
SCHEMA_RELATIONSHIP_PROPERTIES = (
    "MATCH ()-[r]->() WITH type(r) AS type, keys(r) AS ks UNWIND ks AS k RETURN type, collect(DISTINCT k) AS properties ORDER BY type"
)

# A label shared by at least this many distinct sibling labels is treated as a
# generic super-label (e.g. System, Component); its property union is noisy, so we
# render only property names for it rather than typed examples.
GENERIC_LABEL_SIBLING_THRESHOLD = 4


def _scalar_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    return type(value).__name__


def _example_literal(value: Any) -> str:
    try:
        text = json.dumps(to_jsonable(value), default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > 40:
        text = text[:39] + "…"
    return text


def _build_node_section(node_rows: list[dict[str, Any]]) -> list[str]:
    """Render per-label properties with types/examples, trimming generic super-labels.

    For each label we keep the first non-null example value seen for each property.
    Labels shared across many sibling labels (super-labels like ``System``) get a
    names-only listing so their large property unions don't swamp the prompt.
    """
    siblings: dict[str, set[str]] = {}
    examples: dict[str, dict[str, Any]] = {}
    for row in node_rows:
        labels: list[str] = row.get("labels") or []
        props: dict[str, Any] = row.get("props") or {}
        label_set = set(labels)
        for label in labels:
            siblings.setdefault(label, set()).update(label_set - {label})
            store = examples.setdefault(label, {})
            for key, value in props.items():
                if value is not None and key not in store:
                    store[key] = value

    lines = ["Node labels and their properties:"]
    for label in sorted(examples):
        props = examples[label]
        if not props:
            lines.append(f"- {label}: (no properties)")
        elif len(siblings.get(label, set())) >= GENERIC_LABEL_SIBLING_THRESHOLD:
            lines.append(f"- {label}: {', '.join(sorted(props))}")
        else:
            rendered = ", ".join(f"{key} ({_scalar_type(props[key])}, e.g. {_example_literal(props[key])})" for key in sorted(props))
            lines.append(f"- {label}: {rendered}")
    return lines


def fetch_schema_text(driver: Driver, database: str) -> str:
    """Introspect the graph over a synchronous driver and render it as prompt text.

    Uses plain Cypher (no APOC) so it works on a stock Neo4j Community container.
    Node properties are shown with inferred types and example values so the LLM can
    see, for instance, that ``Flight.date`` is an ISO string that needs casting.
    """

    def rows(query: str) -> list[dict[str, Any]]:
        records, _, _ = driver.execute_query(cast(LiteralString, query), database_=database, routing_=RoutingControl.READ)
        return [dict(record) for record in records]

    lines = _build_node_section(rows(SCHEMA_NODE_SAMPLES))

    lines.append("")
    lines.append("Relationships (startLabels)-[TYPE]->(endLabels):")
    for row in rows(SCHEMA_RELATIONSHIPS):
        start = ":".join(row.get("startLabels", []))
        end = ":".join(row.get("endLabels", []))
        lines.append(f"- ({start})-[{row['type']}]->({end})")

    rel_props = rows(SCHEMA_RELATIONSHIP_PROPERTIES)
    if rel_props:
        lines.append("")
        lines.append("Relationship properties:")
        for row in rel_props:
            props = ", ".join(row.get("properties", []))
            lines.append(f"- {row['type']}: {props}")

    return "\n".join(lines)
