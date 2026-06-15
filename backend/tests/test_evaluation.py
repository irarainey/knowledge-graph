"""Unit tests for the deterministic evaluation metrics."""

from __future__ import annotations

from typing import Any

from evaluate import (
    _normalize_whitespace,
    any_intent_matches,
    canonicalize_row,
    document_content,
    intent_match,
    row_matches,
    selected_document_ids,
    unexpected_output_rows,
    unmatched_output_fields,
    unmatched_output_rows,
    value_in_text,
    value_matches,
    values_in_records,
    values_in_text,
)


def test_canonicalize_row_is_key_order_independent() -> None:
    assert canonicalize_row({"a": 1, "b": 2}) == canonicalize_row({"b": 2, "a": 1})


def test_value_in_text_handles_numeric_surface_forms() -> None:
    assert value_in_text(1157, "MTOW is 1,157 kg")
    assert value_in_text(180.0, "produces 180 horsepower")
    assert value_in_text("G-ECHO", "The aircraft g-echo is a Cessna")
    assert not value_in_text(99, "no such number here")


def test_values_in_records_matches_against_raw_rows() -> None:
    records = [{"engine": "Lycoming IO-360", "hp": 180}]
    # Present values are returned; absent values are not.
    assert values_in_records(["Lycoming IO-360", 180], records) == ["Lycoming IO-360", 180]
    assert values_in_records([999], records) == []


def test_values_in_records_is_an_authorization_leak_guard() -> None:
    # A forbidden value must be detected when it appears in the retrieved data.
    records = [{"aerodrome": "Oxford"}]
    assert values_in_records(["Oxford", "Kidlington"], records) == ["Oxford"]
    # Empty data (e.g. a denied fetch) leaks nothing.
    assert values_in_records(["Oxford"], []) == []


def test_normalize_whitespace_collapses_newlines_and_runs() -> None:
    cypher = "MATCH (f:Flight)\nWHERE f.x = 1\n  RETURN  count(f)   AS flights"
    assert _normalize_whitespace(cypher) == "MATCH (f:Flight) WHERE f.x = 1 RETURN count(f) AS flights"
    assert _normalize_whitespace("- A\n- B\n\n- C") == "- A - B - C"
    assert _normalize_whitespace("  padded \t text \n") == "padded text"


# ── intent matching ─────────────────────────────────────────────

# The shape the agent emits (QueryIntent.model_dump(mode="json")): aggregate carries an
# explicit field=None, fields/filters default to empty lists, limit defaults to None.
_COUNT_INTENT: dict[str, Any] = {
    "entity": "Flight",
    "fields": [],
    "filters": [],
    "aggregate": {"func": "count", "field": None},
    "limit": None,
}


def test_intent_match_partial_contract_checks_only_declared_keys() -> None:
    # Declaring just entity + aggregate ignores the incidental fields/filters/limit keys.
    assert intent_match({"entity": "Flight", "aggregate": {"func": "count"}}, _COUNT_INTENT)


def test_intent_match_rejects_wrong_entity() -> None:
    actual = {"entity": "Specification", "fields": ["maxLandingWeight_kg"], "filters": [], "aggregate": None, "limit": None}
    assert not intent_match({"entity": "Aircraft"}, actual)


def test_intent_match_rejects_wrong_aggregate() -> None:
    assert not intent_match({"entity": "Flight", "aggregate": {"func": "sum", "field": "flightTime_hours"}}, _COUNT_INTENT)


def test_intent_match_fields_compared_as_set() -> None:
    actual = {"entity": "Aircraft", "fields": ["maxTakeoffWeight_kg", "registration"], "filters": [], "aggregate": None, "limit": None}
    assert intent_match({"entity": "Aircraft", "fields": ["registration", "maxTakeoffWeight_kg"]}, actual)
    assert not intent_match({"entity": "Aircraft", "fields": ["registration"]}, actual)


def test_intent_match_filters_compared_as_set_of_shapes() -> None:
    actual = {
        "entity": "Flight",
        "fields": [],
        "filters": [{"field": "date", "op": "=", "value": "2026-05-20"}],
        "aggregate": {"func": "count", "field": None},
        "limit": None,
    }
    assert intent_match({"entity": "Flight", "filters": [{"field": "date", "op": "=", "value": "2026-05-20"}]}, actual)
    # A different comparison value is a mismatch.
    assert not intent_match({"entity": "Flight", "filters": [{"field": "date", "op": "=", "value": "2026-05-21"}]}, actual)


def test_intent_match_tolerates_float_formatting() -> None:
    actual = {"entity": "Flight", "filters": [{"field": "distance_nm", "op": ">", "value": 100.0}]}
    assert intent_match({"entity": "Flight", "filters": [{"field": "distance_nm", "op": ">", "value": 100}]}, actual)


def test_any_intent_matches_scans_all_emitted_intents() -> None:
    other: dict[str, Any] = {"entity": "Aerodrome", "fields": [], "filters": [], "aggregate": None, "limit": None}
    expected = {"entity": "Flight", "aggregate": {"func": "count"}}
    assert any_intent_matches(expected, [other, _COUNT_INTENT])
    assert not any_intent_matches(expected, [other])
    assert not any_intent_matches(expected, [])


# ── output scoring (tool output: rows or document content) ───────


def test_values_in_text_returns_present_subset() -> None:
    content = "Never-exceed speed (Vne): 163 KIAS\nMaximum takeoff weight: 1157 kg"
    assert values_in_text(["163", "1157"], content) == ["163", "1157"]
    # A value absent from the content is not reported as present.
    assert values_in_text(["163", "999"], content) == ["163"]
    # Numeric surface forms are matched case-insensitively.
    assert values_in_text([163], content) == [163]


def test_values_in_records_checks_query_output() -> None:
    rows = [{"registration": "G-ECHO", "maxTakeoffWeight_kg": 1157}]
    assert values_in_records(["G-ECHO", "1157"], rows) == ["G-ECHO", "1157"]
    # The wrong entity's output (e.g. standardEmptyWeight_kg) omits the expected value.
    wrong = [{"standardEmptyWeight_kg": 762}]
    assert values_in_records(["G-ECHO", "1157"], wrong) == []


def test_document_content_joins_selected_bodies() -> None:
    docs = [{"documentId": "DOC-0001", "content": "Vne 163 KIAS"}, {"documentId": "DOC-0002", "content": "checklist"}]
    joined = document_content(docs)
    assert "163" in joined and "checklist" in joined
    assert document_content([]) == ""


def test_selected_document_ids_lists_ids_in_order() -> None:
    docs = [{"documentId": "DOC-0001", "content": "x"}, {"documentId": "DOC-0003", "content": "y"}]
    assert selected_document_ids(docs) == ["DOC-0001", "DOC-0003"]
    assert selected_document_ids([]) == []


# ── column-aware output scoring (right value, right field, same row) ──────────


def test_value_matches_uses_exact_equality_not_substring() -> None:
    # Exact canonical equality: 180 must not match a longer number or a superstring.
    assert value_matches(180, 180)
    assert not value_matches(180, 1180)
    assert value_matches("IO-360", "IO-360")
    assert not value_matches("IO-360", "NOT-IO-360-TEST")


def test_value_matches_normalises_numbers() -> None:
    # int / float-int / rounding-noise equivalence (the same tolerance the row F1 uses).
    assert value_matches(1157, 1157.0)
    assert value_matches(6.6, 6.5999999)
    # A string ground-truth value is a different canonical type from a numeric cell.
    assert not value_matches("180", 180)


def test_value_matches_supports_explicit_matchers() -> None:
    assert value_matches({"contains": "Lycoming"}, "Lycoming IO-360")
    assert not value_matches({"contains": "Continental"}, "Lycoming IO-360")
    assert value_matches({"equals": 180}, 180)


def test_row_matches_requires_all_fields_in_the_same_record() -> None:
    record = {"name": "Lycoming IO-360", "model": "IO-360-L2A", "ratedHorsepower": 180}
    assert row_matches({"model": "IO-360-L2A", "ratedHorsepower": 180}, record)
    # A missing field fails the whole spec.
    assert not row_matches({"model": "IO-360-L2A", "cylinders": 4}, record)


def test_unmatched_output_rows_rejects_cross_row_false_positive() -> None:
    # The fact is split across two different engines; no single row carries both, so the
    # combined spec must NOT be considered present (this is the row-level association the
    # flat-substring scorer could not enforce).
    records = [
        {"model": "Wrong", "ratedHorsepower": 180},
        {"model": "IO-360-L2A", "ratedHorsepower": 160},
    ]
    spec = [{"model": "IO-360-L2A", "ratedHorsepower": 180}]
    assert unmatched_output_rows(spec, records) == spec
    # When one row carries both facts together, it matches.
    records.append({"model": "IO-360-L2A", "ratedHorsepower": 180})
    assert unmatched_output_rows(spec, records) == []


def test_unmatched_output_fields_checks_per_column_coverage() -> None:
    records = [
        {"destinationAerodromeName": "Gloucestershire"},
        {"destinationAerodromeName": "Oxford"},
    ]
    assert unmatched_output_fields({"destinationAerodromeName": ["Gloucestershire", "Oxford"]}, records) == {}
    # A value absent from that column is reported as missing for that field only.
    missing = unmatched_output_fields({"destinationAerodromeName": ["Gloucestershire", "Bristol"]}, records)
    assert missing == {"destinationAerodromeName": ["Bristol"]}


def test_unexpected_output_rows_flags_overfetch() -> None:
    # The over-fetch / precision guard: with a closed expected set, any returned record that
    # matches none of the expected rows is surplus data the agent should not have fetched.
    expected = [{"flightResult": 12}]
    # Exactly the expected row -> nothing unexpected.
    assert unexpected_output_rows(expected, [{"flightResult": 12}]) == []
    # A surplus row alongside the right one is flagged.
    surplus = {"flightResult": 99}
    assert unexpected_output_rows(expected, [{"flightResult": 12}, surplus]) == [surplus]


def test_unexpected_output_rows_allows_extra_columns_not_extra_rows() -> None:
    # Projecting extra *columns* is fine (partial-row match); only extra *rows* are surplus.
    expected = [{"pistonEngineModel": "IO-360-L2A", "pistonEngineRatedHorsepower": 180}]
    record = {"pistonEngineName": "Lycoming IO-360", "pistonEngineModel": "IO-360-L2A", "pistonEngineRatedHorsepower": 180}
    assert unexpected_output_rows(expected, [record]) == []
