"""Cypher query-safety checks applied before any generated query runs against the graph.

These are **defence-in-depth** on top of the structured-intent query builder, which only
ever emits read-only ``MATCH … RETURN``. Even a read-only statement can still be expensive
or reach beyond the data (procedures, schema introspection, CSV loads, database switching,
multiple statements). :func:`assert_safe_cypher` rejects those
constructs deterministically — and, crucially, the same function is reused by the
structured-intent query builder, so query safety has a single home.

The check is intentionally conservative and works on the raw query text with string and
backtick-quoted-identifier literals removed first, so a value or label that merely *spells*
a keyword cannot trip it.
"""

from __future__ import annotations

import os
import re

from common import config


class QuerySafetyError(RuntimeError):
    """Raised when a Cypher statement violates a query-safety rule (it is never run)."""


# String literals ('...', "...") and backtick-quoted identifiers (`...`) are stripped
# before keyword scanning so a property value or label that contains a keyword (e.g. a
# node named "Load Test") cannot be mistaken for the construct itself.
_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`(?:[^`\\]|\\.)*`")

# Forbidden constructs. Each entry is (human-readable reason, pattern). Writes are
# included as belt-and-braces even though the read-only EXPLAIN check already blocks them.
_FORBIDDEN: list[tuple[str, re.Pattern[str]]] = [
    ("write clause", re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|FOREACH)\b", re.IGNORECASE)),
    ("procedure call (CALL)", re.compile(r"\bCALL\b", re.IGNORECASE)),
    ("LOAD CSV", re.compile(r"\bLOAD\s+CSV\b", re.IGNORECASE)),
    ("database switch (USE)", re.compile(r"\bUSE\b", re.IGNORECASE)),
    ("schema/admin procedure namespace", re.compile(r"\b(?:db|dbms|apoc|gds|cdc|sys)\.", re.IGNORECASE)),
]

# Leading query-plan keywords the retriever may prepend; stripped before validation so the
# underlying statement is what gets checked.
_PLAN_PREFIX_RE = re.compile(r"^\s*(?:EXPLAIN|PROFILE)\s+", re.IGNORECASE)


def _strip_literals(cypher: str) -> str:
    return _LITERAL_RE.sub("''", cypher)


def strip_plan_prefix(cypher: str) -> str:
    """Remove a leading ``EXPLAIN``/``PROFILE`` so the real statement is validated."""
    return _PLAN_PREFIX_RE.sub("", cypher, count=1)


def assert_safe_cypher(cypher: str) -> None:
    """Raise :class:`QuerySafetyError` if ``cypher`` uses a disallowed construct.

    Rejects empty input, multiple statements, and any of the forbidden constructs above.
    Read-only-ness itself is enforced separately (by the retriever's EXPLAIN check and the
    driver's READ routing); this adds the bounds that check does not.
    """
    statement = strip_plan_prefix(cypher).strip()
    if not statement:
        raise QuerySafetyError("Empty Cypher statement.")

    scannable = _strip_literals(statement)

    # Multiple statements (only a single trailing semicolon is allowed).
    if ";" in scannable.rstrip().rstrip(";"):
        raise QuerySafetyError("Multiple statements are not allowed in a single query.")

    for reason, pattern in _FORBIDDEN:
        if pattern.search(scannable):
            raise QuerySafetyError(f"Disallowed Cypher construct: {reason}.")


def statement_timeout_seconds() -> float:
    """Per-statement timeout (seconds) to bound how long any one query may run."""
    raw = os.getenv(config.ENV_QUERY_TIMEOUT_SECONDS)
    if raw is None:
        return config.QUERY_TIMEOUT_SECONDS_DEFAULT
    try:
        return max(float(raw), 0.1)
    except ValueError:
        return config.QUERY_TIMEOUT_SECONDS_DEFAULT


def row_cap() -> int:
    """Maximum number of rows passed on from a retrieval (bounds context size/leakage)."""
    raw = os.getenv(config.ENV_QUERY_ROW_CAP)
    if raw is None:
        return config.QUERY_ROW_CAP_DEFAULT
    try:
        return max(int(raw), 1)
    except ValueError:
        return config.QUERY_ROW_CAP_DEFAULT


def document_excerpt_char_cap() -> int:
    """Maximum characters of an externalised document body returned to the answer model.

    Bounds the context an externalised (potentially large) document can consume, the same way
    :func:`row_cap` bounds rows. Tunable via ``DOCUMENT_EXCERPT_CHAR_CAP``.
    """
    raw = os.getenv(config.ENV_DOCUMENT_EXCERPT_CHAR_CAP)
    if raw is None:
        return config.DOCUMENT_EXCERPT_CHAR_CAP_DEFAULT
    try:
        return max(int(raw), 1)
    except ValueError:
        return config.DOCUMENT_EXCERPT_CHAR_CAP_DEFAULT
