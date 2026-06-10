"""Tests for the policy-aware structured-intent query builder (Area 1 enforcement).

These include the adversarial authorization cases the plan calls for: direct
restricted-field requests, aggregates over restricted data, filtering on hidden fields,
and classified-row leakage. The query builder is the security boundary, so these assert
that unauthorised data cannot participate in execution — not merely that it is hidden
from the displayed rows.
"""

from __future__ import annotations

import pytest

from authz import (
    Aggregate,
    AggregateFunc,
    AuthorizationError,
    Comparator,
    Filter,
    PolicyStore,
    Principal,
    QueryIntent,
    build_query,
    redact_records,
)

# The real bundled policy is the contract under test.
STORE = PolicyStore.load()


def principal(user: str) -> Principal:
    return STORE.resolve_principal(user)


PUBLIC = principal("public")
MAINTENANCE = principal("maintenance_engineer")
OPS = principal("restricted_ops")


# --- Entity gate -----------------------------------------------------------------------


def test_entity_not_granted_is_denied() -> None:
    # Public may not query Flight at all.
    with pytest.raises(AuthorizationError, match="Flight"):
        build_query(QueryIntent(entity="Flight"), PUBLIC, STORE)


def test_unknown_entity_is_denied() -> None:
    with pytest.raises(AuthorizationError):
        build_query(QueryIntent(entity="Nonexistent"), OPS, STORE)


def test_granted_entity_projects_visible_fields_by_default() -> None:
    built = build_query(QueryIntent(entity="Aircraft"), PUBLIC, STORE)
    assert "MATCH (n:`Aircraft`)" in built.cypher
    assert "n.`registration` AS `registration`" in built.cypher
    # Always-present clearance filter.
    assert "n.classification IS NULL OR n.classification IN" in built.cypher
    assert built.parameters["__authz_classifications"] == ["unclassified"]


# --- Field gate ------------------------------------------------------------------------


def test_maintenance_can_see_flight_duration() -> None:
    built = build_query(QueryIntent(entity="Flight", fields=["blockTime_minutes"]), MAINTENANCE, STORE)
    assert "blockTime_minutes" in built.cypher


def test_ops_can_see_flight_route() -> None:
    built = build_query(QueryIntent(entity="Flight", fields=["destinationAerodrome", "distance_nm"]), OPS, STORE)
    assert "destinationAerodrome" in built.cypher
    assert "distance_nm" in built.cypher


def test_requesting_unknown_field_is_denied_generically() -> None:
    # The error must not confirm whether the field exists; just that it is not permitted.
    with pytest.raises(AuthorizationError, match="not permitted"):
        build_query(QueryIntent(entity="Flight", fields=["militaryMissionCode"]), MAINTENANCE, STORE)


# --- Filter gate (filtering on a hidden field leaks its values) ------------------------


def test_filter_on_hidden_field_is_denied() -> None:
    # Public has only the 'basic' category, so filtering on a 'performance' field of a
    # Specification (which it may query) leaks that field's values and must be denied.
    intent = QueryIntent(
        entity="Specification",
        fields=["name"],
        filters=[Filter(field="maxCruiseSpeed_kt", op=Comparator.GT, value=100)],
    )
    with pytest.raises(AuthorizationError):
        build_query(intent, PUBLIC, STORE)


def test_filter_on_visible_field_is_parameterised() -> None:
    intent = QueryIntent(
        entity="Flight",
        fields=["blockTime_minutes"],
        filters=[Filter(field="date", op=Comparator.GTE, value="2026-06-01")],
    )
    built = build_query(intent, MAINTENANCE, STORE)
    assert "n.`date` >= $p0" in built.cypher
    assert built.parameters["p0"] == "2026-06-01"


# --- Aggregate gate --------------------------------------------------------------------


def test_aggregate_denied_without_grant() -> None:
    # Public has allowAggregates=false.
    intent = QueryIntent(entity="Aircraft", aggregate=Aggregate(func=AggregateFunc.COUNT))
    with pytest.raises(AuthorizationError, match="Aggregate"):
        build_query(intent, PUBLIC, STORE)


def test_count_allowed_for_maintenance() -> None:
    intent = QueryIntent(entity="Flight", aggregate=Aggregate(func=AggregateFunc.COUNT))
    built = build_query(intent, MAINTENANCE, STORE)
    assert "count(n) AS result" in built.cypher
    assert built.aggregated is True
    assert built.returned_fields == ["result"]


def test_aggregate_over_route_field_excludes_classified_for_maintenance() -> None:
    # Route is a clearance-gated category for maintenance: the aggregate is permitted but it
    # must exclude classified rows so military routes/distance never contribute to the result.
    intent = QueryIntent(entity="Flight", aggregate=Aggregate(func=AggregateFunc.AVG, field="distance_nm"))
    built = build_query(intent, MAINTENANCE, STORE)
    assert "avg(n.`distance_nm`) AS result" in built.cypher
    assert "n.classification IN $__authz_classifications" in built.cypher
    assert built.parameters["__authz_classifications"] == ["unclassified"]


def test_aggregate_over_route_field_denied_for_public() -> None:
    # Public has neither aggregates nor the route category, gated or otherwise.
    intent = QueryIntent(entity="Flight", aggregate=Aggregate(func=AggregateFunc.AVG, field="distance_nm"))
    with pytest.raises(AuthorizationError):
        build_query(intent, PUBLIC, STORE)


def test_aggregate_over_visible_field_is_built() -> None:
    intent = QueryIntent(entity="Flight", aggregate=Aggregate(func=AggregateFunc.SUM, field="blockTime_minutes"))
    built = build_query(intent, MAINTENANCE, STORE)
    assert "sum(n.`blockTime_minutes`) AS result" in built.cypher


def test_non_count_aggregate_without_field_is_denied() -> None:
    intent = QueryIntent(entity="Flight", aggregate=Aggregate(func=AggregateFunc.AVG))
    with pytest.raises(AuthorizationError, match="requires a field"):
        build_query(intent, MAINTENANCE, STORE)


# --- Row-level classification (the key anti-inference property) ------------------------


def test_non_gated_principal_gets_whole_row_classification_filter() -> None:
    # Public has no clearance-gated category, so classified rows are excluded wholesale via
    # the injected clearance filter (the standard anti-inference behaviour).
    built = build_query(QueryIntent(entity="Aircraft"), PUBLIC, STORE)
    assert "n.classification IS NULL OR n.classification IN $__authz_classifications" in built.cypher
    assert built.parameters["__authz_classifications"] == ["unclassified"]


def test_secret_clearance_sees_all_classifications() -> None:
    built = build_query(QueryIntent(entity="Flight", aggregate=Aggregate(func=AggregateFunc.COUNT)), OPS, STORE)
    assert built.parameters["__authz_classifications"] == ["unclassified", "official", "secret"]


# --- Clearance-gated categories (maintenance sees classified flights, route redacted) ---


def test_gated_route_field_is_redacted_not_hidden() -> None:
    # Maintenance may project a route field, but its value is redacted (nulled) on rows above
    # its clearance via a CASE — the classified flight row itself stays visible.
    built = build_query(QueryIntent(entity="Flight", fields=["destinationAerodrome"]), MAINTENANCE, STORE)
    assert (
        "CASE WHEN (n.classification IS NULL OR n.classification IN $__authz_classifications) "
        "THEN n.`destinationAerodrome` ELSE null END AS `destinationAerodrome`" in built.cypher
    )
    assert built.parameters["__authz_classifications"] == ["unclassified"]
    # No whole-row classification filter: the row is not hidden, only the field is redacted.
    assert "WHERE" not in built.cypher


def test_non_gated_field_sees_classified_rows_for_maintenance() -> None:
    # A non-gated field (duration) is returned for every flight, including classified ones —
    # this is what lets maintenance see that a classified flight existed and count its hours.
    built = build_query(QueryIntent(entity="Flight", fields=["blockTime_minutes"]), MAINTENANCE, STORE)
    assert "n.`blockTime_minutes` AS `blockTime_minutes`" in built.cypher
    assert "classification" not in built.cypher
    assert "__authz_classifications" not in built.parameters


def test_maintenance_count_includes_classified_rows() -> None:
    # With route clearance-gated, classified flights are no longer hidden from maintenance, so
    # a plain count includes them (the deliberate, audited relaxation) — no clearance filter.
    built = build_query(QueryIntent(entity="Flight", aggregate=Aggregate(func=AggregateFunc.COUNT)), MAINTENANCE, STORE)
    assert "count(n) AS result" in built.cypher
    assert "classification" not in built.cypher
    assert "__authz_classifications" not in built.parameters


def test_aggregate_over_non_gated_field_includes_classified_for_maintenance() -> None:
    # Summing a duration field includes classified flights, giving the true airframe total.
    built = build_query(
        QueryIntent(entity="Flight", aggregate=Aggregate(func=AggregateFunc.SUM, field="flightTime_hours")), MAINTENANCE, STORE
    )
    assert "sum(n.`flightTime_hours`) AS result" in built.cypher
    assert "classification" not in built.cypher
    assert "__authz_classifications" not in built.parameters


def test_filter_on_gated_field_cannot_discover_classified_rows() -> None:
    # Filtering on a route field must not let a classified flight be found by its protected
    # value, so the filter only matches rows within the principal's clearance.
    intent = QueryIntent(
        entity="Flight",
        fields=["name"],
        filters=[Filter(field="destinationAerodrome", op=Comparator.EQ, value="EGLL")],
    )
    built = build_query(intent, MAINTENANCE, STORE)
    assert "(n.classification IS NULL OR n.classification IN $__authz_classifications) AND n.`destinationAerodrome` = $p0)" in built.cypher
    assert built.parameters["p0"] == "EGLL"


def test_ops_route_field_is_not_redacted() -> None:
    # restricted_ops has route as a full (non-gated) grant and secret clearance, so it sees
    # routes directly with the standard whole-row filter — no CASE redaction.
    built = build_query(QueryIntent(entity="Flight", fields=["destinationAerodrome"]), OPS, STORE)
    assert "n.`destinationAerodrome` AS `destinationAerodrome`" in built.cypher
    assert "CASE WHEN" not in built.cypher
    assert "n.classification IS NULL OR n.classification IN $__authz_classifications" in built.cypher


# --- Limit / row cap -------------------------------------------------------------------


def test_limit_is_clamped_to_row_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUERY_ROW_CAP", "5")
    built = build_query(QueryIntent(entity="Aircraft", limit=1000), PUBLIC, STORE)
    assert built.parameters["__authz_limit"] == 5
    assert "LIMIT $__authz_limit" in built.cypher


def test_default_limit_uses_row_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUERY_ROW_CAP", "42")
    built = build_query(QueryIntent(entity="Aircraft"), PUBLIC, STORE)
    assert built.parameters["__authz_limit"] == 42


# --- Redaction (defence-in-depth) ------------------------------------------------------


def test_redaction_strips_unexpected_keys() -> None:
    rows: list[dict[str, object]] = [{"name": "G-ECHO", "classification": "secret", "militaryMissionCode": "X"}]
    redacted = redact_records(rows, ["name"])
    assert redacted == [{"name": "G-ECHO"}]


# --- Temporal versioning (Area 2) ------------------------------------------------------


def test_versioned_entity_defaults_to_current_only() -> None:
    built = build_query(QueryIntent(entity="Specification"), PUBLIC, STORE)
    assert built.versioned is True
    assert built.version_mode == "current"
    assert built.as_of is None
    assert "n.current = true" in built.cypher
    assert "__asOf" not in built.parameters


def test_versioned_entity_as_of_injects_temporal_filter() -> None:
    built = build_query(QueryIntent(entity="Specification"), PUBLIC, STORE, as_of="2020-01-01")
    assert built.versioned is True
    assert built.version_mode == "as-of"
    assert built.as_of == "2020-01-01"
    assert built.parameters["__asOf"] == "2020-01-01"
    assert "n.validFrom <= $__asOf" in built.cypher
    assert "n.validTo IS NULL OR $__asOf < n.validTo" in built.cypher
    assert "n.current = true" not in built.cypher


def test_unversioned_entity_gets_no_temporal_filter() -> None:
    built = build_query(QueryIntent(entity="Aircraft"), PUBLIC, STORE, as_of="2020-01-01")
    assert built.versioned is False
    assert built.as_of is None
    assert "current" not in built.cypher
    assert "validFrom" not in built.cypher
    assert "__asOf" not in built.parameters


def test_event_dated_entity_current_mode_has_no_cutoff() -> None:
    built = build_query(QueryIntent(entity="Flight"), MAINTENANCE, STORE)
    assert built.event_dated is False
    assert built.temporal_filter_applied is False
    assert built.as_of is None
    assert "n.`date` <= $__asOf" not in built.cypher
    assert "__asOf" not in built.parameters


def test_event_dated_entity_as_of_injects_date_cutoff() -> None:
    built = build_query(QueryIntent(entity="Flight"), MAINTENANCE, STORE, as_of="2022-01-01")
    assert built.versioned is False
    assert built.event_dated is True
    assert built.temporal_filter_applied is True
    assert built.as_of == "2022-01-01"
    assert built.parameters["__asOf"] == "2022-01-01"
    assert "n.`date` <= $__asOf" in built.cypher
    # An event-dated entity is not versioned: no version predicate is injected.
    assert "n.current = true" not in built.cypher
    assert "validFrom" not in built.cypher


def test_versioned_filter_composes_with_classification_filter() -> None:
    built = build_query(QueryIntent(entity="Specification"), PUBLIC, STORE)
    assert "n.classification IS NULL" in built.cypher
    assert "n.current = true" in built.cypher
