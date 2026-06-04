"""Cypher-generation prompt for the text-to-Cypher retriever."""

from __future__ import annotations

# Cypher-generation prompt. Replaces the package default so we can inject
# domain rules. Text2CypherRetriever substitutes {schema}, {examples} and
# {query_text}; any other literal braces would need doubling.
CYPHER_GENERATION_PROMPT = """\
Task: write a single read-only Cypher query that answers the user's question using the \
Neo4j graph described below.

Graph schema:
{schema}

Rules:
- Use ONLY the node labels, relationship types and properties that appear in the schema. \
Never invent or guess names. For example, an engine has no "total hours" property — derive \
engine flying hours from the flightTime_hours of the Flights that use the aircraft.
- Inline literal values directly in the query. NEVER use query parameters such as $since or \
$start — the query is executed exactly as written with no parameters supplied.
- Dates are stored as ISO-8601 STRINGS (e.g. "2026-05-20"). For ANY date comparison you MUST \
cast both sides with date(), and use explicit AND for ranges, e.g. \
`WHERE date(f.date) >= date('2026-05-01') AND date(f.date) <= date('2026-05-31')`. \
Never compare Flight.date directly against a date value.
- Systems, components and parts form a hierarchy: \
(:Aircraft)-[:HAS_SYSTEM]->(:System)-[:HAS_COMPONENT]->(component)-[:HAS_PART]->(part). \
To reach a specific component or part, traverse the full path; do not read a property off a \
shortcut node. Only traverse this hierarchy when the question is about a system, component, \
part, engine, tyre or maintenance item. For questions purely about flights, hours or dates, \
query :Flight directly.
- Flights connect to the aircraft via (:Flight)-[:USES_AIRCRAFT]->(:Aircraft) and to their \
phases via (:Flight)-[:HAS_PHASE]->(:FlightPhase). Specific phases also carry labels such as \
:Taxi, :Takeoff and :Landing.
- Wrap nullable numeric properties in coalesce(...) when summing, and add `IS NOT NULL` filters \
for properties that may be absent.
- If the question asks for a count, sum, total, average, maximum or minimum, return that \
aggregate with a clear alias; otherwise return the relevant rows with clear column aliases.

Examples:
{examples}

Question:
{query_text}

Return only the Cypher query, with no backticks, comments or any other text.
"""
