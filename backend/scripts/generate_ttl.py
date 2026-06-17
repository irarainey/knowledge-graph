"""Regenerate the Turtle ontology's instance graph from ``aircraft-knowledge-graph.json``.

``data/aircraft-ontology.ttl`` is made of two parts:

* a hand-written schema header (classes, object properties and datatype
  properties with rich ``rdfs:comment`` / ``rdfs:domain`` / ``rdfs:range``
  axioms), and
* auto-generated sections that *mirror* ``data/aircraft-knowledge-graph.json`` — stub
  declarations for every class, object property and datatype property used by
  the data, followed by the individuals (the instance graph).

The JSON export is the single source of truth for the instances. This script
keeps the hand-written header verbatim and regenerates everything from the
``ADDITIONAL CLASSES`` marker onwards, so the two files never drift apart.

Usage:
    uv run poe generate-ttl                       # rewrite the repo TTL in place
    uv run python scripts/generate_ttl.py --check  # fail if the TTL is stale
    uv run python scripts/generate_ttl.py --json path/to/graph.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Repo layout: <repo>/backend/scripts/generate_ttl.py -> <repo>/data/*
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DEFAULT_JSON_PATH = _DATA_DIR / "aircraft-knowledge-graph.json"
DEFAULT_TTL_PATH = _DATA_DIR / "aircraft-ontology.ttl"

# The hand-written schema ends where the first auto-generated section begins.
_ADDITIONAL_MARKER = "#  ADDITIONAL CLASSES"

# Acronyms that must keep their casing when a SNAKE_CASE relationship type is
# converted to a camelCase predicate (e.g. HAS_PIC -> hasPIC, not hasPic).
_ACRONYMS = {
    "AD",
    "PIC",
    "ATC",
    "GPS",
    "VHF",
    "ELT",
    "ADC",
    "AHRS",
    "AHARS",
    "ICAO",
    "IATA",
    "FIS",
    "ATIS",
    "TWR",
    "APP",
    "GND",
    "RWY",
    "LARS",
    "VFR",
    "IFR",
    "UTC",
    "ILS",
    "VOR",
    "DME",
    "NDB",
    "QNH",
    "QFE",
}


def rel_type_to_predicate(rel_type: str) -> str:
    """Convert a ``SNAKE_CASE`` relationship type to a ``camelCase`` predicate."""
    parts = rel_type.split("_")
    out: list[str] = []
    for index, part in enumerate(parts):
        if part in _ACRONYMS:
            out.append(part)
        elif index == 0:
            out.append(part.lower())
        else:
            out.append(part.capitalize())
    return "".join(out)


def xsd_range(value: Any) -> str:
    """Map a Python scalar to the XSD datatype used for its literal."""
    if isinstance(value, bool):
        return "xsd:boolean"
    if isinstance(value, int):
        return "xsd:integer"
    if isinstance(value, float):
        return "xsd:decimal"
    return "xsd:string"


def _escape_literal(text: str) -> str:
    """Escape a string for a double-quoted Turtle literal (non-ASCII -> \\uXXXX)."""
    out: list[str] = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) > 0x7E:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)


def format_literal(value: Any) -> str:
    """Render a JSON scalar as a typed Turtle literal."""
    if isinstance(value, bool):
        return f'"{str(value).lower()}"^^xsd:boolean'
    if isinstance(value, int):
        return f'"{value}"^^xsd:integer'
    if isinstance(value, float):
        return f'"{value}"^^xsd:decimal'
    return f'"{_escape_literal(str(value))}"'


def parse_header(ttl_text: str) -> str:
    """Return the hand-written schema header, up to the first auto-generated block."""
    idx = ttl_text.find(_ADDITIONAL_MARKER)
    if idx == -1:
        raise ValueError(f"Could not find '{_ADDITIONAL_MARKER}' marker in the TTL file")
    # Rewind to the start of the banner comment line that precedes the marker.
    banner = ttl_text.rfind("# \u2550", 0, idx)
    start = banner if banner != -1 else idx
    return ttl_text[:start].rstrip() + "\n"


def _declared_terms(header: str, keyword: str) -> set[str]:
    """Collect ``spo:Name`` terms declared as ``keyword`` in the header."""
    pattern = re.compile(r"spo:([A-Za-z_]\w*)\s+a\s+[^.;]*" + re.escape(keyword))
    return set(pattern.findall(header))


def _banner(title: str) -> str:
    rule = "\u2550" * 61
    return f"# {rule}\n#  {title}\n# {rule}\n"


def build_additional_classes(nodes: list[dict[str, Any]], base_classes: set[str]) -> str:
    """Declare a class for every label not already in the hand-written schema.

    A label that only ever appears first in a node's ``labels`` is treated as a
    top-level class; a label that appears as a more specific (non-first) label is
    declared ``rdfs:subClassOf`` the base label it sits under.
    """
    parents: dict[str, str] = {}
    order: list[str] = []
    for node in nodes:
        labels = node["labels"]
        for position, label in enumerate(labels):
            if label not in order:
                order.append(label)
            if position > 0 and label not in parents:
                parents[label] = labels[0]

    blocks: list[str] = []
    for label in order:
        if label in base_classes:
            continue
        lines = [f"spo:{label} a owl:Class ;"]
        parent = parents.get(label)
        if parent is not None:
            lines.append(f"    rdfs:subClassOf spo:{parent} ;")
        lines.append(f'    rdfs:label "{_escape_literal(label)}"@en .')
        blocks.append("\n".join(lines))
    return _banner("ADDITIONAL CLASSES  (auto-generated to mirror aircraft-knowledge-graph.json)") + "\n" + "\n\n".join(blocks) + "\n"


def build_additional_object_properties(relationships: list[dict[str, Any]], base_obj: set[str]) -> str:
    """Declare an object property for every relationship type not in the schema."""
    seen: list[str] = []
    for rel in relationships:
        predicate = rel_type_to_predicate(rel["type"])
        if predicate not in seen:
            seen.append(predicate)

    blocks: list[str] = []
    for predicate in seen:
        if predicate in base_obj:
            continue
        blocks.append(f'spo:{predicate} a owl:ObjectProperty ;\n    rdfs:label "{predicate}"@en .')
    return _banner("ADDITIONAL OBJECT PROPERTIES  (auto-generated)") + "\n" + "\n\n".join(blocks) + "\n"


def build_additional_datatype_properties(nodes: list[dict[str, Any]], base_dt: set[str]) -> str:
    """Declare a datatype property (with inferred range) for every data property."""
    ranges: dict[str, str] = {}
    order: list[str] = []
    for node in nodes:
        for key, value in node.get("properties", {}).items():
            if key == "name":
                continue
            if key not in ranges:
                ranges[key] = xsd_range(value)
                order.append(key)

    blocks: list[str] = []
    for key in order:
        if key in base_dt:
            continue
        blocks.append(f'spo:{key} a owl:DatatypeProperty ;\n    rdfs:range {ranges[key]} ;\n    rdfs:label "{key}"@en .')
    return _banner("ADDITIONAL DATATYPE PROPERTIES  (auto-generated)") + "\n" + "\n\n".join(blocks) + "\n"


def build_individuals(nodes: list[dict[str, Any]], relationships: list[dict[str, Any]]) -> str:
    """Emit the instance graph: one Turtle block per node with its outgoing edges."""
    outgoing: dict[str, list[tuple[str, str]]] = {}
    for rel in relationships:
        outgoing.setdefault(rel["startNode"], []).append((rel_type_to_predicate(rel["type"]), rel["endNode"]))

    blocks: list[str] = []
    for node in nodes:
        node_id = node["id"]
        props = node.get("properties", {})
        rdf_type = node["labels"][-1]
        lines = [f"spo:{node_id} a spo:{rdf_type} ;"]

        if "name" in props:
            lines.append(f'    rdfs:label "{_escape_literal(str(props["name"]))}" ;')
        for key, value in props.items():
            if key == "name":
                continue
            lines.append(f"    spo:{key} {format_literal(value)} ;")

        grouped: dict[str, list[str]] = {}
        order: list[str] = []
        for predicate, target in outgoing.get(node_id, []):
            if predicate not in grouped:
                grouped[predicate] = []
                order.append(predicate)
            grouped[predicate].append(target)
        for predicate in order:
            targets = ", ".join(f"spo:{t}" for t in grouped[predicate])
            lines.append(f"    spo:{predicate} {targets} ;")

        # Replace the trailing ';' on the final clause with a '.'
        lines[-1] = lines[-1][:-1].rstrip() + " ."
        blocks.append("\n".join(lines))

    header = _banner("INDIVIDUALS  (instance graph \u2014 generated from aircraft-knowledge-graph.json)")
    return header + "\n" + "\n\n".join(blocks) + "\n"


def render_ttl(graph: dict[str, Any], header: str) -> str:
    """Assemble the full TTL: hand-written header + regenerated mirror sections."""
    nodes = graph["nodes"]
    relationships = graph.get("relationships", [])
    base_classes = _declared_terms(header, "owl:Class")
    base_obj = _declared_terms(header, "owl:ObjectProperty")
    base_dt = _declared_terms(header, "owl:DatatypeProperty")

    sections = [
        header.rstrip() + "\n",
        build_additional_classes(nodes, base_classes),
        build_additional_object_properties(relationships, base_obj),
        build_additional_datatype_properties(nodes, base_dt),
        build_individuals(nodes, relationships),
    ]
    return "\n\n".join(section.rstrip() + "\n" for section in sections)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Regenerate the TTL instance graph from the graph JSON.")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON_PATH, help="Path to the graph JSON export")
    parser.add_argument("--ttl", type=Path, default=DEFAULT_TTL_PATH, help="Path to the TTL ontology to rewrite")
    parser.add_argument("--check", action="store_true", help="Exit non-zero if the TTL is out of date instead of writing")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    graph = json.loads(args.json.read_text(encoding="utf-8"))
    header = parse_header(args.ttl.read_text(encoding="utf-8"))
    rendered = render_ttl(graph, header)

    if args.check:
        current = args.ttl.read_text(encoding="utf-8")
        if current != rendered:
            print(f"{args.ttl} is out of date; run 'uv run poe generate-ttl'.", file=sys.stderr)
            return 1
        print(f"{args.ttl} is up to date.")
        return 0

    args.ttl.write_text(rendered, encoding="utf-8")
    print(f"Wrote {args.ttl} ({len(graph['nodes'])} nodes, {len(graph.get('relationships', []))} relationships).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
