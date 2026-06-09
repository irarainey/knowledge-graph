"""Unit tests for the Cypher query-safety checks."""

from __future__ import annotations

import pytest

from common.query_safety import (
    QuerySafetyError,
    assert_safe_cypher,
    row_cap,
    statement_timeout_seconds,
    strip_plan_prefix,
)

_SAFE = [
    "MATCH (f:Flight) RETURN count(f) AS flights",
    "MATCH (a:Aircraft)-[:HAS_SYSTEM]->(s:System) RETURN s.name AS system LIMIT 25",
    "MATCH (f:Flight) WHERE f.date >= '2026-05-01' RETURN f.name AS name, f.blockTime_minutes AS mins",
]

_UNSAFE = [
    ("CREATE (n:Hacked) RETURN n", "write"),
    ("MATCH (n) DETACH DELETE n", "write"),
    ("MATCH (n) SET n.x = 1 RETURN n", "write"),
    ("CALL db.labels()", "procedure"),
    ("CALL apoc.help('x')", "procedure"),
    ("MATCH (n) RETURN n; MATCH (m) RETURN m", "Multiple"),
    ("LOAD CSV FROM 'file:///x.csv' AS row RETURN row", "LOAD CSV"),
    ("USE neo4j MATCH (n) RETURN n", "USE"),
    ("MATCH (n) RETURN dbms.components()", "schema/admin"),
    ("", "Empty"),
]


@pytest.mark.parametrize("cypher", _SAFE)
def test_safe_queries_pass(cypher: str) -> None:
    assert_safe_cypher(cypher)  # does not raise


@pytest.mark.parametrize(("cypher", "needle"), _UNSAFE)
def test_unsafe_queries_rejected(cypher: str, needle: str) -> None:
    with pytest.raises(QuerySafetyError) as exc:
        assert_safe_cypher(cypher)
    assert needle.lower() in str(exc.value).lower()


def test_explain_prefix_is_stripped_before_validation() -> None:
    assert strip_plan_prefix("EXPLAIN MATCH (n) RETURN n") == "MATCH (n) RETURN n"
    # An EXPLAIN over a safe statement is still safe; over an unsafe one still rejected.
    assert_safe_cypher("EXPLAIN MATCH (n) RETURN n")
    with pytest.raises(QuerySafetyError):
        assert_safe_cypher("EXPLAIN CALL db.labels()")


def test_keyword_inside_string_literal_does_not_trip() -> None:
    # A value that merely contains a keyword must not be mistaken for the construct.
    assert_safe_cypher("MATCH (d:Document) WHERE d.title = 'How to CREATE a flight plan' RETURN d")
    assert_safe_cypher('MATCH (n) WHERE n.note = "please DELETE later" RETURN n')


def test_trailing_semicolon_is_allowed() -> None:
    assert_safe_cypher("MATCH (n) RETURN n;")


def test_timeout_and_row_cap_read_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUERY_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("QUERY_ROW_CAP", "50")
    assert statement_timeout_seconds() == 3.5
    assert row_cap() == 50


def test_timeout_and_row_cap_fall_back_on_bad_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUERY_TIMEOUT_SECONDS", "not-a-number")
    monkeypatch.setenv("QUERY_ROW_CAP", "nope")
    assert statement_timeout_seconds() == 10.0
    assert row_cap() == 1000
