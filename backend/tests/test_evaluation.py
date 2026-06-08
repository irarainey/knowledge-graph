"""Unit tests for the deterministic evaluation metrics."""

from __future__ import annotations

from evaluate import (
    _normalize_whitespace,
    answer_metrics,
    canonicalize_row,
    mean,
    retrieval_metrics,
    value_in_text,
)


def test_canonicalize_row_is_key_order_independent() -> None:
    assert canonicalize_row({"a": 1, "b": 2}) == canonicalize_row({"b": 2, "a": 1})


def test_retrieval_metrics_perfect_match() -> None:
    rows = [{"name": "A"}, {"name": "B"}]
    metrics = retrieval_metrics(rows, list(reversed(rows)))
    assert metrics.precision == 1.0
    assert metrics.recall == 1.0
    assert metrics.f1 == 1.0
    assert metrics.jaccard == 1.0
    assert metrics.exact_match is True


def test_retrieval_metrics_partial_overlap() -> None:
    predicted = [{"x": 1}, {"x": 2}, {"x": 3}]
    gold = [{"x": 2}, {"x": 3}, {"x": 4}]
    metrics = retrieval_metrics(predicted, gold)
    assert metrics.true_positives == 2
    assert metrics.false_positives == 1
    assert metrics.false_negatives == 1
    assert metrics.precision == 2 / 3
    assert metrics.recall == 2 / 3
    assert metrics.f1 == 2 / 3
    assert metrics.jaccard == 2 / 4
    assert metrics.exact_match is False


def test_retrieval_metrics_both_empty_is_perfect() -> None:
    metrics = retrieval_metrics([], [])
    assert metrics.f1 == 1.0
    assert metrics.exact_match is True


def test_retrieval_metrics_empty_gold_nonempty_prediction() -> None:
    metrics = retrieval_metrics([{"x": 1}], [])
    assert metrics.precision == 0.0
    assert metrics.recall == 1.0
    assert metrics.f1 == 0.0
    assert metrics.exact_match is False


def test_retrieval_metrics_deduplicates_rows() -> None:
    metrics = retrieval_metrics([{"x": 1}, {"x": 1}], [{"x": 1}])
    assert metrics.predicted_count == 1
    assert metrics.exact_match is True


def test_retrieval_metrics_ignores_column_names() -> None:
    # Same values, different aliases -> value-based match, but strict mismatch.
    predicted = [{"horsepower": 180, "engine": "IO-360"}]
    gold = [{"hp": 180, "name": "IO-360"}]
    metrics = retrieval_metrics(predicted, gold)
    assert metrics.exact_match is True
    assert metrics.f1 == 1.0
    assert metrics.strict_exact_match is False
    assert metrics.strict_f1 == 0.0


def test_retrieval_metrics_tolerates_float_formatting() -> None:
    metrics = retrieval_metrics([{"hours": 6.5999999999}], [{"hours": 6.6}])
    assert metrics.exact_match is True
    assert metrics.f1 == 1.0


def test_retrieval_metrics_detects_wrong_value_despite_alias() -> None:
    # G-ECHO matches; 2550 (lb) != 1157 (kg) is a genuine miss.
    predicted = [{"registration": "G-ECHO", "maxTakeoffWeight_lb": 2550}]
    gold = [{"registration": "G-ECHO", "mtow_kg": 1157}]
    metrics = retrieval_metrics(predicted, gold)
    assert metrics.precision == 0.5
    assert metrics.recall == 0.5
    assert metrics.exact_match is False


def test_retrieval_metrics_answer_key_restricts_gold_columns() -> None:
    # Gold has only the answer column; generated adds descriptive columns.
    gold = [{"component": "Exhaust"}, {"component": "Propeller"}]
    predicted = [
        {"system": "Powerplant", "component": "Exhaust"},
        {"system": "Powerplant", "component": "Propeller"},
    ]
    without_key = retrieval_metrics(predicted, gold)
    assert without_key.exact_match is False  # extra "Powerplant" value dings precision
    with_key = retrieval_metrics(predicted, gold, answer_key="component")
    assert with_key.exact_match is True
    assert with_key.f1 == 1.0


def test_retrieval_metrics_round_digits_controls_tolerance() -> None:
    loose = retrieval_metrics([{"v": 6.61}], [{"v": 6.6}], ndigits=1)
    assert loose.exact_match is True
    strict = retrieval_metrics([{"v": 6.61}], [{"v": 6.6}], ndigits=3)
    assert strict.exact_match is False


def test_value_in_text_handles_numeric_surface_forms() -> None:
    assert value_in_text(1157, "MTOW is 1,157 kg")
    assert value_in_text(180.0, "produces 180 horsepower")
    assert value_in_text("G-ECHO", "The aircraft g-echo is a Cessna")
    assert not value_in_text(99, "no such number here")


def test_answer_metrics_coverage_and_groundedness() -> None:
    answer = "The engine is a Lycoming IO-360 producing 180 hp."
    records = [{"engine": "Lycoming IO-360", "hp": 180}]
    metrics = answer_metrics(answer, ["Lycoming IO-360", 180], records)
    assert metrics.coverage == 1.0
    assert metrics.groundedness == 1.0
    assert metrics.missing == []
    assert metrics.ungrounded == []


def test_answer_metrics_missing_value() -> None:
    metrics = answer_metrics("Only mentions IO-360.", ["IO-360", 200], [{"model": "IO-360"}])
    assert metrics.matched == ["IO-360"]
    assert metrics.missing == [200]
    assert metrics.coverage == 0.5


def test_answer_metrics_flags_ungrounded_value() -> None:
    # The answer states a fact that is not present in the retrieved rows.
    metrics = answer_metrics("There are 12 flights.", [12], [{"flights": 7}])
    assert metrics.matched == [12]
    assert metrics.ungrounded == [12]
    assert metrics.groundedness == 0.0


def test_answer_metrics_no_expected_values_is_not_applicable() -> None:
    metrics = answer_metrics("anything", [], [{"x": 1}])
    assert metrics.coverage is None
    assert metrics.groundedness is None


def test_mean_ignores_none() -> None:
    assert mean([1.0, None, 3.0]) == 2.0
    assert mean([None, None]) is None


def test_normalize_whitespace_collapses_newlines_and_runs() -> None:
    cypher = "MATCH (f:Flight)\nWHERE f.x = 1\n  RETURN  count(f)   AS flights"
    assert _normalize_whitespace(cypher) == "MATCH (f:Flight) WHERE f.x = 1 RETURN count(f) AS flights"
    assert _normalize_whitespace("- A\n- B\n\n- C") == "- A - B - C"
    assert _normalize_whitespace("  padded \t text \n") == "padded text"
