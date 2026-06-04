"""Few-shot examples for cypher generation."""

from __future__ import annotations

# Few-shot question/Cypher pairs that anchor the cypher-generation LLM to this
# graph's exact labels, relationship types and conventions: ISO-string dates cast
# with date(), inlined literals (no $parameters), full hierarchy traversal for
# component/part questions, and aggregation for totals.
DEFAULT_EXAMPLES = [
    "USER INPUT: 'How many flights are recorded?' QUERY: MATCH (f:Flight) RETURN count(f) AS flights",
    "USER INPUT: 'Which aerodromes has the aircraft flown to?' "
    "QUERY: MATCH (:Flight)-[:ARRIVES_AT]->(a:Aerodrome) RETURN DISTINCT a.name AS aerodrome",
    "USER INPUT: 'How many flying hours has the engine had since 2026-05-25?' "
    "QUERY: MATCH (ac:Aircraft)-[:HAS_SYSTEM]->(:System)-[:HAS_COMPONENT]->(e:PistonEngine) "
    "MATCH (f:Flight)-[:USES_AIRCRAFT]->(ac) WHERE date(f.date) >= date('2026-05-25') "
    "RETURN e.name AS engine, count(f) AS flights, sum(coalesce(f.flightTime_hours, 0)) AS hours",
    "USER INPUT: 'How much ground distance has the front tyre covered between 2026-05-01 and 2026-05-31?' "
    "QUERY: MATCH (f:Flight)-[:USES_AIRCRAFT]->(:Aircraft)-[:HAS_SYSTEM]->(:LandingGearSystem)"
    "-[:HAS_COMPONENT]->(:NoseWheel)-[:HAS_PART]->(tyre:Tyre) "
    "WHERE date(f.date) >= date('2026-05-01') AND date(f.date) <= date('2026-05-31') "
    "MATCH (f)-[:HAS_PHASE]->(phase:FlightPhase) WHERE phase.groundRoll_m IS NOT NULL "
    "RETURN tyre.name AS tyre, sum(phase.groundRoll_m) AS totalGroundDistance_m",
]
