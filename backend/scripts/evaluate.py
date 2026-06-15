"""Offline evaluation of the ``/ask`` knowledge-graph pipeline.

Runs each question from a pre-baked ground-truth file through the in-process
:class:`KnowledgeGraphAgent` (the same code path the API uses), then scores the
result with deterministic metrics — no LLM-as-judge:

* **Output** — the single source of truth. The *content* the chosen tool returned is
  checked against **hand-written known answers** in the ground truth. For a query the rows
  are checked column-aware against the agent's deterministic, entity-qualified output
  columns: ``expected_output_rows`` asserts the right value in the right field within a
  single record, and ``expected_output_fields`` asserts per-column coverage across records.
  ``exact_output`` additionally fails the case if the agent returned any row *outside* the
  expected set (an over-fetch / precision guard). For a document fetch the selected
  document's content is substring-matched against ``expected_output_values`` and the chosen
  document can be pinned (``expected_document_id``).
* **Intent** — the structured query intent the model emitted (entity/fields/filters/
  aggregate) is matched against an ``expected_intent`` in the ground truth, scoring the
  model's retrieval *decision* (what to fetch) separately from the rows it returned.
* **Tool selection** — did the agent invoke exactly the expected tool(s)?
* **Operational** — query validity, empty-retrieval rate, and the token/latency
  cost taken from the pipeline's own ``stats`` debug event.

The known-answer values are derived once, by hand, from the seeded graph; the queries that
produced them are kept in ``eval/ground_truth_provenance.json`` for traceability (they are
reference only and are never executed by this harness — the ground truth is fixed data, not
a second live query run against the same database).

Results are written as a single JSON document (per-question detail + run-level
aggregates) and a summary is logged.

Usage:
    uv run poe evaluate
    uv run python scripts/evaluate.py --ground-truth eval/ground_truth.json
    uv run python scripts/evaluate.py --question-id flight-count --output -
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# This script lives in backend/scripts, outside the ``src`` package. Add ``src`` to
# the path so the shared modules import when the script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agents import AzureOpenAISettings, KnowledgeGraphAgent
from authz import PolicyStore, Principal
from common.config import ENV_LOG_LEVEL, LOG_LEVEL_DEFAULT
from common.env import load_env
from common.logging_config import get_logger, setup_logging
from neo4j_client import Neo4jClient, Neo4jSettings

logger = get_logger(__name__)

# Repo layout: <repo>/backend/scripts/evaluate.py -> <repo>/backend/eval/ground_truth.json
BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GROUND_TRUTH = BACKEND_ROOT / "eval" / "ground_truth.json"
DEFAULT_RESULTS_DIR = BACKEND_ROOT / "eval" / "results"


# ── Deterministic metrics ───────────────────────────────────────
# Pure, side-effect-free scoring (no LLM-as-judge). Evaluation deliberately scores the
# parts of the pipeline that have a single correct, machine-checkable answer:
#   1. **tool selection** — did the agent pick the right tool(s)?
#   2. **intent selection** — did the model emit the right structured query intent
#      (entity/fields/filters/aggregate), matched against the case's ``expected_intent``?
#   3. **query validity** — did a query/fetch actually run without error?
#   4. **tool output** — does the content the chosen tool returned carry the case's
#      hand-written known answer: for a query, the right value in the right column (and same
#      row) via ``expected_output_rows`` / ``expected_output_fields`` — and, when
#      ``exact_output`` is set, *no* rows outside the expected set (over-fetch guard); for a
#      document fetch, the ``expected_output_values`` in its content and the
#      ``expected_document_id`` selected?
# The final natural-language answer is intentionally NOT scored: judging its wording for
# correctness would need an LLM-as-judge (non-deterministic, costly). The generated prose is
# just a presentation layer over the retrieved data; the data is the real test. The answer
# text is still recorded in the report for human review, but never gates pass/fail.

DEFAULT_ROUND_DIGITS = 3


def _canonical(value: Any) -> Any:
    """Recursively normalise a value into order-independent, JSON-ready data."""
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in value}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def canonicalize_row(row: Any) -> str:
    """Return a stable, hashable JSON string for a record (used for strict matching).

    Keys are sorted so that two records with the same fields in a different order
    compare equal; non-JSON scalars fall back to ``str`` so canonicalisation never
    raises on unexpected driver types.
    """
    return json.dumps(_canonical(row), sort_keys=True, ensure_ascii=False, default=str)


def _round_floats(value: Any, ndigits: int) -> Any:
    """Recursively round floats so e.g. ``6.5999999`` and ``6.6`` compare equal."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        rounded = round(value, ndigits)
        return int(rounded) if rounded.is_integer() else rounded
    if isinstance(value, Mapping):
        return {str(key): _round_floats(value[key], ndigits) for key in value}
    if isinstance(value, (list, tuple)):
        return [_round_floats(item, ndigits) for item in value]
    return value


def _canonicalize_value(value: Any, ndigits: int) -> str:
    """Canonical, hashable string for a single cell value (float-tolerant)."""
    return json.dumps(_round_floats(_canonical(value), ndigits), sort_keys=True, ensure_ascii=False, default=str)


def _value_variants(value: Any) -> set[str]:
    """Surface forms a fact value might take in free text (e.g. ``1157`` / ``1,157``)."""
    variants = {str(value)}
    if isinstance(value, bool):
        return variants
    if isinstance(value, float) and value.is_integer():
        variants.add(str(int(value)))
    if isinstance(value, int):
        variants.add(f"{value:,}")
    return {variant for variant in variants if variant}


def value_in_text(value: Any, text: str) -> bool:
    """Case-insensitive check that any surface form of ``value`` appears in ``text``."""
    haystack = text.casefold()
    return any(variant.casefold() in haystack for variant in _value_variants(value))


def values_in_records(values: Sequence[Any], records: Iterable[Any]) -> list[Any]:
    """Return the subset of ``values`` that appear in the *raw retrieved rows*.

    Matches against the canonicalised record data (not the natural-language answer), so the
    check is a deterministic test of what the query actually returned. Used both to confirm
    expected facts are present in the data and to assert forbidden facts are absent from it.
    """
    record_text = canonicalize_row(list(records))
    return [value for value in values if value_in_text(value, record_text)]


def values_in_text(values: Sequence[Any], text: str) -> list[Any]:
    """Return the subset of ``values`` that appear in ``text`` (e.g. document content)."""
    return [value for value in values if value_in_text(value, text)]


def value_matches(expected: Any, actual: Any, *, ndigits: int = DEFAULT_ROUND_DIGITS) -> bool:
    """Whether a single retrieved cell ``actual`` satisfies an ``expected`` ground-truth value.

    Structured record cells are compared by **exact canonical equality** (not substring), so a
    field assertion proves the value, not a coincidental overlap: ``180`` does not match
    ``1180`` and ``"IO-360"`` does not match ``"NOT-IO-360"``. Numbers are normalised so
    ``1157``/``1157.0`` and float-rounding noise compare equal (the same tolerance the row F1
    uses). Substring/``contains`` matching is opt-in via an explicit matcher object so it can
    never be the silent default:

    * ``{"contains": "Lycoming"}`` — case-insensitive substring of the cell's text form.
    * ``{"equals": <value>}`` — explicit exact equality (the default for a bare value).
    """
    if isinstance(expected, Mapping):
        if "contains" in expected:
            return value_in_text(expected["contains"], str(actual))
        if "equals" in expected:
            expected = expected["equals"]
        else:
            raise ValueError(f"Unsupported output matcher {expected!r} (use 'equals' or 'contains').")
    return _canonicalize_value(expected, ndigits) == _canonicalize_value(actual, ndigits)


def row_matches(spec: Mapping[str, Any], record: Mapping[str, Any], *, ndigits: int = DEFAULT_ROUND_DIGITS) -> bool:
    """Whether one retrieved ``record`` satisfies a partial-row ``spec``.

    Every field named in ``spec`` must be present in the *same* record and match its expected
    value. Requiring all the fields in a single record is what preserves row-level association
    (so ``model=IO-360`` and ``hp=180`` must describe the *same* engine, not two different
    rows that each happen to carry one of the facts).
    """
    return all(
        field_name in record and value_matches(expected, record[field_name], ndigits=ndigits) for field_name, expected in spec.items()
    )


def unmatched_output_rows(
    expected_rows: Sequence[Mapping[str, Any]], records: Sequence[Mapping[str, Any]], *, ndigits: int = DEFAULT_ROUND_DIGITS
) -> list[Mapping[str, Any]]:
    """Return the expected partial-rows that no retrieved record satisfies (empty = all found)."""
    return [spec for spec in expected_rows if not any(row_matches(spec, record, ndigits=ndigits) for record in records)]


def unexpected_output_rows(
    expected_rows: Sequence[Mapping[str, Any]], records: Sequence[Mapping[str, Any]], *, ndigits: int = DEFAULT_ROUND_DIGITS
) -> list[Mapping[str, Any]]:
    """Return the retrieved records that satisfy *none* of the expected partial-rows.

    This is the over-fetch / precision guard: with the ground truth's ``expected_output_rows``
    enumerating the complete known answer set, any returned record that matches no expected
    spec is a row the agent should not have fetched (it pulled extra or wrong data). Matching
    is partial-row (a record only needs to satisfy *some* expected spec on the spec's named
    fields), so legitimately projecting extra *columns* is fine — only extra *rows* are flagged.
    Returned empty when every record is accounted for. Only meaningful for ``exact_output`` cases.
    """
    return [record for record in records if not any(row_matches(spec, record, ndigits=ndigits) for spec in expected_rows)]


def unmatched_output_fields(
    expected_fields: Mapping[str, Any], records: Sequence[Mapping[str, Any]], *, ndigits: int = DEFAULT_ROUND_DIGITS
) -> dict[str, list[Any]]:
    """Per-column coverage check, ignoring row identity.

    For each ``field -> expected`` entry (``expected`` is a single value or a list of values),
    every expected value must appear somewhere in *that column* across the retrieved records.
    Returns ``{field: [values still missing]}`` for any field with gaps (empty = all covered).
    Use this for independent multi-row coverage (e.g. a set of aerodrome names in one column);
    use ``expected_output_rows`` when fields must co-occur in the same row.
    """
    missing: dict[str, list[Any]] = {}
    for field_name, expected in expected_fields.items():
        wanted = list(expected) if isinstance(expected, list) else [expected]
        column = [record[field_name] for record in records if field_name in record]
        absent = [value for value in wanted if not any(value_matches(value, cell, ndigits=ndigits) for cell in column)]
        if absent:
            missing[field_name] = absent
    return missing


def output_set_counts(
    expected_rows: Sequence[Mapping[str, Any]],
    expected_fields: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    ndigits: int = DEFAULT_ROUND_DIGITS,
) -> tuple[int, int, int] | None:
    """True/false positive/negative counts for **set-overlap** scoring of query output.

    Treats the retrieved rows as a *retrieval set* compared to the ground-truth answer set, so
    precision/recall/F1 can be derived (see :func:`prf_from_counts`). Returns ``None`` when the
    case declares no row/field output expectation (nothing to score against).

    Row specs (``expected_output_rows``) count one expected answer per spec: a **true positive**
    is an expected spec satisfied by some record, a **false negative** an expected spec no record
    satisfies, and a **false positive** a returned record that satisfies no expected spec (the
    over-fetch signal). Field specs (``expected_output_fields``) count per-column *values*: a
    true positive is an expected value present in its column, a false negative an expected value
    missing, and a false positive a distinct value in that column that was *not* expected — so
    precision over field specs assumes the listed values enumerate the column's complete set.
    """
    if not expected_rows and not expected_fields:
        return None
    tp = fp = fn = 0
    if expected_rows:
        unmatched_specs = unmatched_output_rows(expected_rows, records, ndigits=ndigits)
        fn += len(unmatched_specs)
        tp += len(expected_rows) - len(unmatched_specs)
        fp += len(unexpected_output_rows(expected_rows, records, ndigits=ndigits))
    if expected_fields:
        for field_name, expected in expected_fields.items():
            wanted = list(expected) if isinstance(expected, list) else [expected]
            column = [record[field_name] for record in records if field_name in record]
            distinct_actual: list[Any] = []
            for cell in column:
                if not any(value_matches(seen, cell, ndigits=ndigits) for seen in distinct_actual):
                    distinct_actual.append(cell)
            for value in wanted:
                if any(value_matches(value, cell, ndigits=ndigits) for cell in column):
                    tp += 1
                else:
                    fn += 1
            for cell in distinct_actual:
                if not any(value_matches(value, cell, ndigits=ndigits) for value in wanted):
                    fp += 1
    return tp, fp, fn


def prf_from_counts(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    """Derive (precision, recall, F1) from true/false positive/negative counts.

    Precision is ``tp / (tp + fp)`` (how much of what was returned was expected), recall is
    ``tp / (tp + fn)`` (how much of the expected answer was returned), and F1 their harmonic
    mean. A zero denominator (nothing returned / nothing expected) yields ``0.0`` by convention.
    """
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return precision, recall, f1


def document_content(documents: Iterable[Mapping[str, Any]]) -> str:
    """Concatenate the content of the selected documents into one searchable string.

    The document tool's *output* is the body of the document it chose; joining the bodies of
    every document fetched in a turn gives a single haystack to check expected output values
    against — the document-side equivalent of the rows a query returns.
    """
    return "\n".join(str(doc.get("content", "")) for doc in documents)


def selected_document_ids(documents: Iterable[Mapping[str, Any]]) -> list[str]:
    """The ids of the documents the agent selected, in order (for which-document scoring)."""
    return [str(doc["documentId"]) for doc in documents if doc.get("documentId") is not None]


def intent_match(expected: Any, actual: Any, *, ndigits: int = DEFAULT_ROUND_DIGITS) -> bool:
    """Whether the LLM-emitted query ``intent`` satisfies the ground-truth ``expected`` intent.

    The comparison is value-based and order-independent, and treats ``expected`` as a partial
    contract: only the keys the ground truth declares are checked, so a case can assert (say)
    the entity and aggregate without pinning every field. ``None`` values in ``expected`` are
    skipped (declare a key only to require it). Scalars are compared with float tolerance;
    lists (``fields``/``filters``) are compared as sets so element order does not matter and a
    filter's serialised shape (``{"field": ..., "op": "=", "value": ...}``) must match exactly.
    """
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        return all(intent_match(value, actual.get(key), ndigits=ndigits) for key, value in expected.items() if value is not None)
    if isinstance(expected, (list, tuple)):
        if not isinstance(actual, (list, tuple)):
            return False
        return {_canonicalize_value(item, ndigits) for item in expected} == {_canonicalize_value(item, ndigits) for item in actual}
    return _canonicalize_value(expected, ndigits) == _canonicalize_value(actual, ndigits)


def any_intent_matches(expected: Any, intents: Sequence[Any], *, ndigits: int = DEFAULT_ROUND_DIGITS) -> bool:
    """True if any of the emitted ``intents`` satisfies the ``expected`` intent contract."""
    return any(intent_match(expected, intent, ndigits=ndigits) for intent in intents)


def _normalize_whitespace(text: str) -> str:
    """Collapse all runs of whitespace (incl. newlines) into single spaces and strip.

    Applied to the generated/gold query and the answer text in the report so they are
    stored as single, readable lines free of the model's formatting and line breaks.
    """
    return " ".join(text.split())


# ── Ground truth + orchestration ────────────────────────────────


@dataclass(frozen=True)
class GoldQuestion:
    """One evaluation case.

    Three kinds of case are supported, distinguished by :meth:`mode` (driven by the declared
    ``expected_tools``). None of them score the natural-language answer — only tool selection,
    intent, query validity and the tool's *output* against a hand-written known answer:

    * **retrieval** — a graph question (``expected_tools`` includes ``query_knowledge_graph``).
      The rows the agent fetched are scored against the case's hand-written
      ``expected_output_rows`` / ``expected_output_fields`` (column-aware known answers), with
      an optional ``exact_output`` over-fetch guard. This is the real test.
    * **document** — a question whose content lives outside the graph (``expected_tools``
      includes ``fetch_document_content``). Scored on selecting the right tool, the fetch
      actually returning a document, and — when the case declares them — the *output* of that
      fetch: the right document was selected (``expected_document_id``) and its content carries
      the expected facts (``expected_output_values``). The answer wording is never scored (that
      would need an LLM-as-judge); the document the tool returned is.
    * **refusal** — an authorization case (``expect_refusal``) where the *correct* outcome is
      that the request is denied; scored on the pipeline actually denying (a typed
      authorization denial in the audit trail) and no ``forbidden_answer_values`` reaching the
      retrieved data.

    ``user`` drives the case as a specific identity (defaulting to the run identity), and
    ``as_of`` evaluates it against the graph as it existed on that date — so authorization and
    temporal behaviour can be evaluated, not just retrieval quality.

    ``expected_tools`` lists the tool name(s) a correct answer should invoke (e.g.
    ``["query_knowledge_graph"]`` or ``["fetch_document_content"]``); required for every
    non-refusal case. It both selects the case's :meth:`mode` and is checked against the tools
    the agent actually invoked — picking the right tool is part of being correct.

    ``forbidden_answer_values`` (optional) are values that must NOT appear in the retrieved
    data (a deterministic data-leak guard for authorization cases, e.g. a clearance-gated
    aerodrome that should have been nulled out of the rows).

    ``expected_intent`` (optional, retrieval cases) is the structured query intent a correct
    answer should emit (entity/fields/filters/aggregate/limit). It is matched value-based and
    order-independent against the intent the LLM actually produced — scoring the model's
    *retrieval decision* (what to fetch), separately from the rows it returned. Declared keys
    form a partial contract (see :func:`intent_match`), so a case can pin just the parts that
    matter (e.g. entity + aggregate) without over-fitting to incidental fields.

    ``expected_output_values`` (optional, document cases) are values that must appear in the
    document tool's *output* — i.e. the selected document's content (a case-insensitive
    substring check, since a document body has no columns). For retrieval cases the tool's
    output is rows, so it is scored more precisely by ``expected_output_rows`` /
    ``expected_output_fields`` below instead.

    ``expected_output_rows`` (optional, retrieval cases) is the hand-written known answer: a
    list of *partial rows* the retrieved data must contain. Each spec must be satisfied by a
    single returned record where **every** named field matches (exact canonical equality,
    numeric-tolerant; ``{"contains": ...}`` opts into substring). Keys are the agent's
    deterministic, entity-qualified output columns — ``<entityCamel><FieldPascal>`` for a
    projection (``pistonEngineRatedHorsepower``), ``<entityCamel>Result`` for an aggregate
    (``flightResult``), or ``<codeAlias>Name`` for a resolved aerodrome companion
    (``flightDestinationAerodromeName``). This is the precise "right value in the right field,
    in the same row" check (see :func:`row_matches`).

    ``exact_output`` (optional, retrieval cases, default ``false``) turns ``expected_output_rows``
    into a *closed set*: the case additionally fails if the agent returned any record matching
    none of the expected rows (an over-fetch / precision guard). Set it when the expected rows
    enumerate the complete answer (e.g. a single aggregate value or the one matching node);
    leave it off for open-ended coverage scored via ``expected_output_fields``.

    ``expected_output_fields`` (optional, retrieval cases) asserts per-column coverage without
    pinning row identity: ``{column: value | [values]}`` requires each value to appear in that
    column across the returned records (see :func:`unmatched_output_fields`). Use it for
    independent multi-row sets (e.g. a list of aerodrome names) where rows need not co-occur.

    ``expected_document_id`` (optional, document cases) is the id of the document a correct
    answer should select — scoring *which* document the tool returned.

    The known-answer values are fixed data, derived once by hand from the seeded graph; the
    derivation queries live in ``eval/ground_truth_provenance.json`` for traceability and are
    never executed here.
    """

    id: str
    question: str
    user: str | None = None
    as_of: str | None = None
    expect_refusal: bool = False
    forbidden_answer_values: list[Any] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    expected_intent: dict[str, Any] | None = None
    expected_output_values: list[Any] = field(default_factory=list)
    expected_output_rows: list[dict[str, Any]] = field(default_factory=list)
    expected_output_fields: dict[str, Any] = field(default_factory=dict)
    exact_output: bool = False
    expected_document_id: str | None = None

    @property
    def mode(self) -> str:
        if self.expect_refusal:
            return "refusal"
        if "fetch_document_content" in self.expected_tools:
            return "document"
        return "retrieval"


def load_ground_truth(path: Path) -> list[GoldQuestion]:
    """Load and minimally validate the ground-truth file."""
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    raw_questions = data.get("questions") if isinstance(data, dict) else data
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ValueError(f"{path} does not contain a non-empty 'questions' list")

    questions: list[GoldQuestion] = []
    seen: set[str] = set()
    for entry in raw_questions:
        for key in ("id", "question"):
            if not entry.get(key):
                raise ValueError(f"Ground-truth entry is missing required field '{key}': {entry!r}")
        expect_refusal = bool(entry.get("expect_refusal", False))
        # Every non-refusal case must declare expected_tools: it both selects the case mode
        # (document vs retrieval) and is the tool the agent is required to invoke. A refusal
        # case needs neither a tool nor an output expectation.
        if not expect_refusal and not entry.get("expected_tools"):
            raise ValueError(
                f"Ground-truth entry {entry['id']!r} must declare 'expected_tools' "
                "(retrieval or document case), or 'expect_refusal' (authorization case)"
            )
        if entry["id"] in seen:
            raise ValueError(f"Duplicate question id in ground truth: {entry['id']!r}")
        seen.add(entry["id"])
        _validate_output_expectations(entry, expect_refusal)
        questions.append(
            GoldQuestion(
                id=entry["id"],
                question=entry["question"],
                user=entry.get("user"),
                as_of=entry.get("as_of"),
                expect_refusal=expect_refusal,
                forbidden_answer_values=list(entry.get("forbidden_answer_values", [])),
                expected_tools=list(entry.get("expected_tools", [])),
                expected_intent=entry.get("expected_intent"),
                expected_output_values=list(entry.get("expected_output_values", [])),
                expected_output_rows=list(entry.get("expected_output_rows", [])),
                expected_output_fields=dict(entry.get("expected_output_fields", {})),
                exact_output=bool(entry.get("exact_output", False)),
                expected_document_id=entry.get("expected_document_id"),
            )
        )
    return questions


def _validate_output_expectations(entry: Mapping[str, Any], expect_refusal: bool) -> None:
    """Enforce that each output expectation is used in the mode it was designed for.

    The tool's output is shaped differently per mode: a retrieval case returns columnar rows
    (scored precisely by ``expected_output_rows`` / ``expected_output_fields``), while a
    document case returns free text (scored by ``expected_output_values`` substring). Mixing
    them is a fixture mistake, so reject it loudly rather than silently never matching.
    """
    tools = entry.get("expected_tools") or []
    is_document = not expect_refusal and "fetch_document_content" in tools
    is_retrieval = not expect_refusal and not is_document
    eid = entry["id"]
    record_keys = ("expected_output_rows", "expected_output_fields")
    if entry.get("expected_output_values") and not is_document:
        raise ValueError(
            f"Ground-truth entry {eid!r}: 'expected_output_values' (document-content substring check) "
            "is only valid on a document case; a retrieval case must use 'expected_output_rows'."
        )
    for key in record_keys:
        if entry.get(key) and not is_retrieval:
            raise ValueError(f"Ground-truth entry {eid!r}: {key!r} is only valid on a retrieval case.")
    if entry.get("exact_output") and not entry.get("expected_output_rows"):
        raise ValueError(f"Ground-truth entry {eid!r}: 'exact_output' requires 'expected_output_rows' to enumerate the answer.")
    rows = entry.get("expected_output_rows") or []
    if not isinstance(rows, list) or any(not isinstance(spec, dict) or not spec for spec in rows):
        raise ValueError(f"Ground-truth entry {eid!r}: 'expected_output_rows' must be a list of non-empty objects.")
    fields = entry.get("expected_output_fields") or {}
    if not isinstance(fields, dict):
        raise ValueError(f"Ground-truth entry {eid!r}: 'expected_output_fields' must be an object keyed by output column.")


async def _drive_agent(agent: KnowledgeGraphAgent, question: str, principal: Principal, *, as_of: str | None = None) -> dict[str, Any]:
    """Consume the agent's streamed events into the pieces the metrics need."""
    answer_parts: list[str] = []
    cypher_used: list[str] = []
    records: list[dict[str, Any]] = []
    tools_used: list[str] = []
    intents_used: list[dict[str, Any]] = []
    documents_used: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    error: str | None = None

    async for event in agent.ask(question, principal=principal, as_of=as_of):
        kind = event.get("type")
        if kind == "metadata":
            cypher_used = event.get("cypher_used", [])
            records = event.get("records", [])
            tools_used = event.get("tools_used", [])
            intents_used = event.get("intents_used", [])
            documents_used = event.get("documents_used", [])
        elif kind == "token":
            answer_parts.append(event.get("text", ""))
        elif kind == "stats":
            stats = event
        elif kind == "error":
            error = event.get("message")

    return {
        "answer": "".join(answer_parts),
        "cypher_used": cypher_used,
        "records": records,
        "tools_used": tools_used,
        "intents_used": intents_used,
        "documents_used": documents_used,
        "stats": stats,
        "error": error,
    }


async def evaluate_question(
    agent: KnowledgeGraphAgent,
    question: GoldQuestion,
    *,
    policy: PolicyStore,
    default_principal: Principal,
    round_digits: int = DEFAULT_ROUND_DIGITS,
) -> dict[str, Any]:
    """Run one question through the pipeline and score it according to its mode."""
    principal = policy.resolve_principal(question.user) if question.user else default_principal
    logger.info(
        "Evaluating '%s' (mode=%s, as=%s%s): %s",
        question.id,
        question.mode,
        principal.id,
        f", as_of={question.as_of}" if question.as_of else "",
        question.question,
    )

    outcome = await _drive_agent(agent, question.question, principal, as_of=question.as_of)
    answer_text = _normalize_whitespace(outcome["answer"])
    records = outcome["records"]
    documents = outcome["documents_used"]
    stats = outcome["stats"]
    audit_denied = (stats.get("audit") or {}).get("denied") or []
    # Data-leak guard: forbidden values are checked against the *retrieved rows*, not the
    # answer prose — authorization is enforced on the data, so the data is where a leak shows.
    leaked = values_in_records(question.forbidden_answer_values, records)

    generated_query = _normalize_whitespace(outcome["cypher_used"][0]) if outcome["cypher_used"] else None
    query_valid = generated_query is not None and outcome["error"] is None
    empty_retrieval = len(records) == 0
    # Refusal is detected from the deterministic authorization signal (a typed denial recorded
    # in the audit trail), never from reading the answer prose.
    refused = bool(audit_denied)

    # Intent selection: the structured query intent the LLM emitted (entity/fields/filters/
    # aggregate) is the model's retrieval *decision*, scored separately from the rows it
    # returned. When the case declares ``expected_intent`` (retrieval cases only), require one
    # of the emitted intents to satisfy it (value-based, order-independent partial match).
    intents_used = list(outcome["intents_used"])
    intent_selection_ok: bool | None = None
    if question.expected_intent is not None:
        intent_selection_ok = any_intent_matches(question.expected_intent, intents_used, ndigits=round_digits)

    # Tool selection: now the agent chooses between the graph-query and document-content
    # tools, so picking the right one is part of being correct. When the case declares
    # ``expected_tools``, require the agent to have invoked exactly that set (order-insensitive).
    tools_used = list(outcome["tools_used"])
    tool_selection_ok: bool | None = None
    if question.expected_tools:
        tool_selection_ok = set(tools_used) == set(question.expected_tools)

    # Output scoring: evaluate what the chosen tool actually *returned* against fixed
    # ground-truth values — and, for a query, in the right column/row. This is the value-of-the-
    # output check that complements tool/intent selection: it asserts the answer's source data
    # is right, not just that the right tool ran.
    doc_ids = selected_document_ids(documents)
    missing_output_values: list[Any] = []
    missing_output_rows: list[Mapping[str, Any]] = []
    missing_output_fields: dict[str, list[Any]] = {}
    unexpected_rows: list[Mapping[str, Any]] = []
    output_values_ok: bool | None = None
    # The document body the substring check actually ran against (the scored haystack),
    # recorded so the dashboard can show *what* was evaluated, not just the document id.
    document_content_text: str | None = None
    present_output_values: list[Any] = []
    if question.mode == "document":
        # A document body has no columns, so its content is scored by substring presence.
        document_content_text = document_content(documents)
        present = values_in_text(question.expected_output_values, document_content_text)
        present_output_values = list(present)
        missing_output_values = [value for value in question.expected_output_values if value not in present]
        if question.expected_output_values:
            output_values_ok = not missing_output_values
    else:
        # Query rows are columnar, so the output is scored against the agent's deterministic
        # output columns: partial rows must match within a single record (right value, right
        # field, same row); field specs check per-column coverage across records.
        if question.expected_output_rows:
            missing_output_rows = unmatched_output_rows(question.expected_output_rows, records, ndigits=round_digits)
            # Over-fetch / precision guard: with a closed expected set, any returned record
            # outside it means the agent pulled extra or wrong rows.
            if question.exact_output:
                unexpected_rows = unexpected_output_rows(question.expected_output_rows, records, ndigits=round_digits)
        if question.expected_output_fields:
            missing_output_fields = unmatched_output_fields(question.expected_output_fields, records, ndigits=round_digits)
        if question.expected_output_rows or question.expected_output_fields:
            output_values_ok = not missing_output_rows and not missing_output_fields and not unexpected_rows
    # Set-overlap scoring (precision/recall/F1) over the same expected rows/fields — a
    # continuous diagnostic that complements the pass/fail output check (only meaningful for
    # retrieval cases, whose output is a columnar set; a document body has no comparable set).
    output_counts = (
        output_set_counts(question.expected_output_rows, question.expected_output_fields, records, ndigits=round_digits)
        if question.mode != "document"
        else None
    )
    # Which-document selection (document cases): did the tool return the expected document?
    document_id_ok: bool | None = None
    if question.expected_document_id is not None:
        document_id_ok = question.expected_document_id in doc_ids

    # Pass criteria differ by mode. None of them inspect the natural-language answer.
    if question.mode == "refusal":
        # Correct when the pipeline denied the request AND no forbidden value reached the data.
        passed = refused and not leaked
    elif question.mode == "document":
        # Correct when the right tool was chosen, the fetch ran without error and actually
        # returned a document (non-empty rows), and the request was not wrongly denied/leaked.
        passed = query_valid and not empty_retrieval and not refused and not leaked
    else:  # retrieval
        # Correct when the query ran without error and no forbidden value leaked; the hand-
        # written output expectations (enforced just below) are what verify the actual values.
        passed = query_valid and not refused and not leaked

    # The wrong intent fails a retrieval case: the model fetched the wrong thing, even if the
    # rows happened to score. Only enforced when the case declares an expected_intent.
    if intent_selection_ok is False:
        passed = False

    # The tool's output must carry the expected values, and a document case must have selected
    # the expected document. Only enforced when the case declares the corresponding expectation.
    if output_values_ok is False:
        passed = False
    if document_id_ok is False:
        passed = False

    # The wrong tool fails the case (a refusal may legitimately deny before any tool runs).
    if tool_selection_ok is False and question.mode != "refusal":
        passed = False

    tools_label = "ok" if tool_selection_ok else ("BAD" if tool_selection_ok is False else "-")
    intent_label = "ok" if intent_selection_ok else ("BAD" if intent_selection_ok is False else "-")
    output_label = "ok" if output_values_ok else ("BAD" if output_values_ok is False else "-")
    if question.mode == "retrieval":
        logger.info(
            "  %s | valid=%s rows=%d overfetch=%d leaked=%s tools=%s intent=%s output=%s",
            "PASS" if passed else "FAIL",
            query_valid,
            len(records),
            len(unexpected_rows),
            bool(leaked),
            tools_label,
            intent_label,
            output_label,
        )
    else:
        doc_label = "ok" if document_id_ok else ("BAD" if document_id_ok is False else "-")
        logger.info(
            "  %s | mode=%s refused=%s rows=%d leaked=%s tools=%s output=%s doc=%s",
            "PASS" if passed else "FAIL",
            question.mode,
            refused,
            len(records),
            bool(leaked),
            tools_label,
            output_label,
            doc_label,
        )

    cost = {
        "model": stats.get("model"),
        "llm_calls": stats.get("llm_calls"),
        "tokens": stats.get("tokens", {}),
        "durations_ms": stats.get("durations_ms", {}),
    }

    return {
        "id": question.id,
        "question": question.question,
        "mode": question.mode,
        "user": principal.id,
        "as_of": question.as_of,
        "passed": passed,
        "refused": refused,
        "leaked_values": leaked,
        "tools_used": tools_used,
        "expected_tools": question.expected_tools,
        "tool_selection_ok": tool_selection_ok,
        "expected_intent": question.expected_intent,
        "intents_used": intents_used,
        "intent_selection_ok": intent_selection_ok,
        "expected_output_values": question.expected_output_values,
        "present_output_values": present_output_values,
        "missing_output_values": missing_output_values,
        "expected_output_rows": question.expected_output_rows,
        "missing_output_rows": missing_output_rows,
        "expected_output_fields": question.expected_output_fields,
        "missing_output_fields": missing_output_fields,
        "exact_output": question.exact_output,
        "unexpected_output_rows": unexpected_rows,
        "output_values_ok": output_values_ok,
        # Set-overlap diagnostics (None when the case declares no row/field output to score).
        "output_tp": output_counts[0] if output_counts else None,
        "output_fp": output_counts[1] if output_counts else None,
        "output_fn": output_counts[2] if output_counts else None,
        "output_precision": prf_from_counts(*output_counts)[0] if output_counts else None,
        "output_recall": prf_from_counts(*output_counts)[1] if output_counts else None,
        "output_f1": prf_from_counts(*output_counts)[2] if output_counts else None,
        "expected_document_id": question.expected_document_id,
        "documents_used": doc_ids,
        "document_id_ok": document_id_ok,
        # The fetched document body the expected_output_values were substring-matched against,
        # persisted so the dashboard can surface the *evaluated* content (document cases only).
        "document_content": document_content_text,
        "query_valid": query_valid,
        "empty_retrieval": empty_retrieval,
        "error": outcome["error"],
        "generated_query": generated_query,
        "predicted_record_count": len(records),
        # The rows the query actually returned, recorded for human review (the redacted,
        # entity-aliased output the agent saw); scoring compares these to expected_output_*.
        "records": records,
        # The answer text is recorded for human review only — it is never scored.
        "answer_text": answer_text,
        "cost": cost,
    }


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll per-question metrics up into a summary block.

    Operational metrics (query validity, empty-retrieval) are averaged over retrieval-mode
    questions; the document-fetch rate over document cases; authorization, tool-selection,
    intent and output metrics over the cases that exercise them. The natural-language answer is
    never scored.
    """
    count = len(results)
    if count == 0:
        return {"question_count": 0}

    retrieval_results = [r for r in results if r["mode"] == "retrieval"]
    retrieval_count = len(retrieval_results)
    token_totals = [r["cost"]["tokens"].get("total") for r in results]
    latency_totals = [r["cost"]["durations_ms"].get("total") for r in results]

    total_tokens = sum(value for value in token_totals if value is not None)
    total_latency_ms = sum(value for value in latency_totals if value is not None)

    summary: dict[str, Any] = {
        "question_count": count,
        "pass_rate": sum(r["passed"] for r in results) / count,
        "retrieval_question_count": retrieval_count,
        "query_valid_rate": (sum(r["query_valid"] for r in retrieval_results) / retrieval_count) if retrieval_count else None,
        "empty_retrieval_rate": (sum(r["empty_retrieval"] for r in retrieval_results) / retrieval_count) if retrieval_count else None,
        "overfetch_count": sum(1 for r in retrieval_results if r["unexpected_output_rows"]),
        "total_tokens": total_tokens,
        "total_latency_ms": round(total_latency_ms, 1),
        "mean_latency_ms": round(total_latency_ms / count, 1),
    }

    # Document signal: of the document cases, how many actually fetched a document (non-empty),
    # and — where the case pinned it — how often the *expected* document was selected.
    document_cases = [r for r in results if r["mode"] == "document"]
    if document_cases:
        summary["document_question_count"] = len(document_cases)
        summary["document_fetch_rate"] = sum(not r["empty_retrieval"] for r in document_cases) / len(document_cases)
    document_id_cases = [r for r in results if r["document_id_ok"] is not None]
    if document_id_cases:
        summary["document_selection_accuracy"] = sum(r["document_id_ok"] for r in document_id_cases) / len(document_id_cases)

    # Authorization signal: of the cases that should refuse, how many did; and across all
    # cases, did any forbidden value reach the retrieved data (should always be zero).
    refusal_cases = [r for r in results if r["mode"] == "refusal"]
    if refusal_cases:
        summary["refusal_correct_rate"] = sum(r["refused"] for r in refusal_cases) / len(refusal_cases)
    leak_cases = [r for r in results if r["leaked_values"]]
    summary["forbidden_leak_count"] = len(leak_cases)

    # Tool selection: over the cases that declared expected_tools, how often did the agent
    # invoke exactly the right tool(s)?
    tool_cases = [r for r in results if r["tool_selection_ok"] is not None]
    if tool_cases:
        summary["tool_selection_question_count"] = len(tool_cases)
        summary["tool_selection_accuracy"] = sum(r["tool_selection_ok"] for r in tool_cases) / len(tool_cases)

    # Intent selection: over the cases that declared expected_intent, how often did the model
    # emit a structured intent matching the expected one?
    intent_cases = [r for r in results if r["intent_selection_ok"] is not None]
    if intent_cases:
        summary["intent_selection_question_count"] = len(intent_cases)
        summary["intent_selection_accuracy"] = sum(r["intent_selection_ok"] for r in intent_cases) / len(intent_cases)

    # Output values: over the cases that declared expected_output_values, how often did the
    # tool's output (rows or document content) carry all of them?
    output_cases = [r for r in results if r["output_values_ok"] is not None]
    if output_cases:
        summary["output_value_question_count"] = len(output_cases)
        summary["output_value_accuracy"] = sum(r["output_values_ok"] for r in output_cases) / len(output_cases)

    # Set-overlap quality: precision/recall/F1 over the retrieval cases whose output is a
    # scorable row/field set. Reported both macro (mean of per-case scores — every case counts
    # equally) and micro (from pooled tp/fp/fn — every expected/returned item counts equally).
    prf_cases = [r for r in results if r["output_f1"] is not None]
    if prf_cases:
        n = len(prf_cases)
        summary["output_set_question_count"] = n
        summary["output_precision_macro"] = sum(r["output_precision"] for r in prf_cases) / n
        summary["output_recall_macro"] = sum(r["output_recall"] for r in prf_cases) / n
        summary["output_f1_macro"] = sum(r["output_f1"] for r in prf_cases) / n
        tp = sum(r["output_tp"] for r in prf_cases)
        fp = sum(r["output_fp"] for r in prf_cases)
        fn = sum(r["output_fn"] for r in prf_cases)
        micro_p, micro_r, micro_f1 = prf_from_counts(tp, fp, fn)
        summary["output_precision_micro"] = micro_p
        summary["output_recall_micro"] = micro_r
        summary["output_f1_micro"] = micro_f1
    return summary


def build_report(results: list[dict[str, Any]], *, ground_truth: Path) -> dict[str, Any]:
    """Assemble the full evaluation report document."""
    models = {r["cost"].get("model") for r in results if r["cost"].get("model")}
    return {
        "run": {
            "timestamp": datetime.now(UTC).isoformat(),
            "ground_truth": str(ground_truth),
            "model": next(iter(models)) if len(models) == 1 else sorted(models),
            "question_count": len(results),
        },
        "summary": _aggregate(results),
        "questions": results,
    }


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def log_summary(report: dict[str, Any]) -> None:
    """Log a compact, human-readable summary of the run."""
    summary = report["summary"]
    logger.info("=" * 60)
    logger.info("Evaluation summary (%d questions)", summary.get("question_count", 0))
    logger.info("  pass rate           : %s", _fmt(summary.get("pass_rate")))
    logger.info("  query valid rate    : %s", _fmt(summary.get("query_valid_rate")))
    logger.info("  empty retrieval rate: %s", _fmt(summary.get("empty_retrieval_rate")))
    if "output_value_accuracy" in summary:
        logger.info("  output value acc    : %s", _fmt(summary.get("output_value_accuracy")))
    if "output_f1_macro" in summary:
        logger.info(
            "  output precision    : %s (macro) / %s (micro)",
            _fmt(summary.get("output_precision_macro")),
            _fmt(summary.get("output_precision_micro")),
        )
        logger.info(
            "  output recall       : %s (macro) / %s (micro)",
            _fmt(summary.get("output_recall_macro")),
            _fmt(summary.get("output_recall_micro")),
        )
        logger.info(
            "  output F1           : %s (macro) / %s (micro)", _fmt(summary.get("output_f1_macro")), _fmt(summary.get("output_f1_micro"))
        )
    logger.info("  over-fetch cases    : %s", summary.get("overfetch_count", 0))
    if "tool_selection_accuracy" in summary:
        logger.info("  tool selection acc  : %s", _fmt(summary.get("tool_selection_accuracy")))
    if "intent_selection_accuracy" in summary:
        logger.info("  intent selection acc: %s", _fmt(summary.get("intent_selection_accuracy")))
    if "document_fetch_rate" in summary:
        logger.info("  document fetch rate : %s", _fmt(summary.get("document_fetch_rate")))
    if "document_selection_accuracy" in summary:
        logger.info("  document select acc : %s", _fmt(summary.get("document_selection_accuracy")))
    if "refusal_correct_rate" in summary:
        logger.info("  refusal correct rate: %s", _fmt(summary.get("refusal_correct_rate")))
    logger.info("  forbidden leaks     : %s", summary.get("forbidden_leak_count", 0))
    logger.info("  total tokens        : %s", summary.get("total_tokens"))
    logger.info("  mean latency (ms)   : %s", summary.get("mean_latency_ms"))
    logger.info("=" * 60)


def _write_report(report: dict[str, Any], output: str | None) -> None:
    """Write the report to a file (or stdout when ``output`` is ``-``)."""
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if output == "-":
        print(payload)
        return
    if output:
        path = Path(output)
    else:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = DEFAULT_RESULTS_DIR / f"eval-{timestamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload + "\n", encoding="utf-8")
    logger.info("Wrote evaluation report to %s", path)


async def run(args: argparse.Namespace) -> int:
    questions = load_ground_truth(args.ground_truth)
    if args.question_id:
        questions = [q for q in questions if q.id in set(args.question_id)]
    if not questions:
        logger.error("No questions matched the given filters")
        return 1

    neo4j_settings = Neo4jSettings.from_env()
    client = Neo4jClient(neo4j_settings)
    # Pre-flight connectivity check so the run fails fast (with a clear error) if the graph the
    # agent will query is unreachable — the harness itself no longer issues any gold queries.
    await client.verify_connectivity()
    await client.close()
    policy = PolicyStore.load()
    # Drive the agent as the configured identity — defaulting to the most-privileged one so
    # authorization does not mask retrieval-quality regressions. Use ``--user`` to scope it.
    if args.user is not None:
        principal = policy.resolve_principal(args.user)
    else:
        most_privileged = max(policy.list_identities(), key=lambda identity: policy.clearance_rank(identity.clearance))
        principal = policy.resolve_principal(most_privileged.id)
    logger.info(
        "Driving the pipeline as default identity '%s' (clearance=%s); per-question 'user' overrides it", principal.id, principal.clearance
    )
    agent = KnowledgeGraphAgent.from_settings(AzureOpenAISettings.from_env(), neo4j_settings, policy)

    try:
        results = [
            await evaluate_question(
                agent,
                question,
                policy=policy,
                default_principal=principal,
                round_digits=args.round_digits,
            )
            for question in questions
        ]
    finally:
        agent.close()

    report = build_report(results, ground_truth=args.ground_truth)
    log_summary(report)
    _write_report(report, args.output)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline evaluation of the /ask knowledge-graph pipeline.")
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GROUND_TRUTH, help="Path to the ground-truth JSON file")
    parser.add_argument(
        "--output",
        default=None,
        help="Output path for the JSON report; '-' writes to stdout (default: eval/results/eval-<timestamp>.json)",
    )
    parser.add_argument(
        "--round-digits",
        type=int,
        default=DEFAULT_ROUND_DIGITS,
        help="Decimal places to round floats to before value comparison (float tolerance)",
    )
    parser.add_argument("--question-id", action="append", default=None, help="Only evaluate this question id (repeatable)")
    parser.add_argument("--env-file", type=Path, default=None, help="Path to a .env file with Neo4j/Azure OpenAI settings")
    parser.add_argument(
        "--user",
        default=None,
        help="Identity to drive the pipeline as (defaults to the policy's most-privileged identity)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env(args.env_file)
    setup_logging(level=os.getenv(ENV_LOG_LEVEL, LOG_LEVEL_DEFAULT))
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
