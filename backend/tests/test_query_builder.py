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
    Direction,
    Filter,
    PolicyStore,
    Principal,
    QueryIntent,
    RelationshipHop,
    attach_aerodrome_names,
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
    assert "n.`registration` AS `aircraftRegistration`" in built.cypher
    # Always-present clearance filter.
    assert "n.classification IS NULL OR n.classification IN" in built.cypher
    assert built.parameters["__authz_classifications"] == ["unclassified"]


def test_projection_alias_is_entity_qualified_eval_contract() -> None:
    # CONTRACT: the builder aliases every projected field as an entity-qualified camelCase
    # output column (``n.`field` AS `<entityCamel><FieldPascal>``) and every aggregate as
    # ``<entityCamel>Result``. The offline evaluation harness keys its column-aware output
    # expectations (expected_output_rows / expected_output_fields in eval/ground_truth.json)
    # on exactly these deterministic output column names. If this aliasing ever changes, those
    # expectations would silently stop matching — so this test locks the alias scheme the eval
    # depends on.
    built = build_query(QueryIntent(entity="Aircraft", fields=["registration", "maxTakeoffWeight_kg"]), PUBLIC, STORE)
    for field_name, alias in (("registration", "aircraftRegistration"), ("maxTakeoffWeight_kg", "aircraftMaxTakeoffWeight_kg")):
        assert f"n.`{field_name}` AS `{alias}`" in built.cypher
        assert alias in built.returned_fields
    counted = build_query(QueryIntent(entity="Flight", aggregate=Aggregate(func=AggregateFunc.COUNT)), MAINTENANCE, STORE)
    assert "count(n) AS `flightResult`" in counted.cypher
    assert counted.returned_fields == ["flightResult"]


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
    assert "count(n) AS `flightResult`" in built.cypher
    assert built.aggregated is True
    assert built.returned_fields == ["flightResult"]


def test_aggregate_over_route_field_excludes_classified_for_maintenance() -> None:
    # Route is a clearance-gated category for maintenance: the aggregate is permitted but it
    # must exclude classified rows so military routes/distance never contribute to the result.
    intent = QueryIntent(entity="Flight", aggregate=Aggregate(func=AggregateFunc.AVG, field="distance_nm"))
    built = build_query(intent, MAINTENANCE, STORE)
    assert "avg(n.`distance_nm`) AS `flightResult`" in built.cypher
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
    assert "sum(n.`blockTime_minutes`) AS `flightResult`" in built.cypher


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
        "THEN n.`destinationAerodrome` ELSE null END AS `flightDestinationAerodrome`" in built.cypher
    )
    assert built.parameters["__authz_classifications"] == ["unclassified"]
    # No whole-row classification filter: the row is not hidden, only the field is redacted.
    assert "WHERE" not in built.cypher


def test_non_gated_field_sees_classified_rows_for_maintenance() -> None:
    # A non-gated field (duration) is returned for every flight, including classified ones —
    # this is what lets maintenance see that a classified flight existed and count its hours.
    built = build_query(QueryIntent(entity="Flight", fields=["blockTime_minutes"]), MAINTENANCE, STORE)
    assert "n.`blockTime_minutes` AS `flightBlockTime_minutes`" in built.cypher
    assert "classification" not in built.cypher
    assert "__authz_classifications" not in built.parameters


def test_maintenance_count_includes_classified_rows() -> None:
    # With route clearance-gated, classified flights are no longer hidden from maintenance, so
    # a plain count includes them (the deliberate, audited relaxation) — no clearance filter.
    built = build_query(QueryIntent(entity="Flight", aggregate=Aggregate(func=AggregateFunc.COUNT)), MAINTENANCE, STORE)
    assert "count(n) AS `flightResult`" in built.cypher
    assert "classification" not in built.cypher
    assert "__authz_classifications" not in built.parameters


def test_aggregate_over_non_gated_field_includes_classified_for_maintenance() -> None:
    # Summing a duration field includes classified flights, giving the true airframe total.
    built = build_query(
        QueryIntent(entity="Flight", aggregate=Aggregate(func=AggregateFunc.SUM, field="flightTime_hours")), MAINTENANCE, STORE
    )
    assert "sum(n.`flightTime_hours`) AS `flightResult`" in built.cypher
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
    assert "n.`destinationAerodrome` AS `flightDestinationAerodrome`" in built.cypher
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


_AERODROME_NAMES = {"EGGD": "Bristol", "EGBP": "Cotswold Airport"}


def test_attach_aerodrome_names_adds_resolved_name_beside_code() -> None:
    records: list[dict[str, object]] = [{"flightDestinationAerodrome": "EGGD"}, {"flightDestinationAerodrome": "EGBP"}]
    columns = {"flightDestinationAerodrome": "flightDestinationAerodromeName"}
    enriched = attach_aerodrome_names(records, columns, _AERODROME_NAMES)
    assert enriched[0] == {"flightDestinationAerodrome": "EGGD", "flightDestinationAerodromeName": "Bristol"}
    assert enriched[1] == {"flightDestinationAerodrome": "EGBP", "flightDestinationAerodromeName": "Cotswold Airport"}


def test_attach_aerodrome_names_resolves_both_departure_and_destination() -> None:
    records: list[dict[str, object]] = [{"flightDepartureAerodrome": "EGGD", "flightDestinationAerodrome": "EGBP"}]
    columns = {
        "flightDepartureAerodrome": "flightDepartureAerodromeName",
        "flightDestinationAerodrome": "flightDestinationAerodromeName",
    }
    enriched = attach_aerodrome_names(records, columns, _AERODROME_NAMES)
    assert enriched[0]["flightDepartureAerodromeName"] == "Bristol"
    assert enriched[0]["flightDestinationAerodromeName"] == "Cotswold Airport"


def test_attach_aerodrome_names_keeps_redacted_code_nameless() -> None:
    # A gated route nulled on a classified row must not gain a name (no redaction bypass).
    records: list[dict[str, object]] = [{"flightDestinationAerodrome": None}]
    columns = {"flightDestinationAerodrome": "flightDestinationAerodromeName"}
    enriched = attach_aerodrome_names(records, columns, _AERODROME_NAMES)
    assert enriched[0] == {"flightDestinationAerodrome": None, "flightDestinationAerodromeName": None}


def test_attach_aerodrome_names_unknown_code_resolves_to_none() -> None:
    records: list[dict[str, object]] = [{"flightDestinationAerodrome": "ZZZZ"}]
    columns = {"flightDestinationAerodrome": "flightDestinationAerodromeName"}
    enriched = attach_aerodrome_names(records, columns, _AERODROME_NAMES)
    assert enriched[0]["flightDestinationAerodromeName"] is None


def test_attach_aerodrome_names_noop_without_aerodrome_fields() -> None:
    records: list[dict[str, object]] = [{"name": "Flight 001", "flightTime_hours": 1.2}]
    enriched = attach_aerodrome_names(records, {}, _AERODROME_NAMES)
    assert enriched == [{"name": "Flight 001", "flightTime_hours": 1.2}]


def test_requested_name_companion_field_is_canonicalised_to_code() -> None:
    # The model sometimes asks for the synthetic "<code>Name" companion in its fields; the
    # builder must project the underlying code field rather than rejecting it as unknown.
    built = build_query(QueryIntent(entity="Flight", fields=["name", "destinationAerodromeName"]), MAINTENANCE, STORE)
    assert "n.`destinationAerodromeName`" not in built.cypher
    assert "destinationAerodrome` ELSE null END AS `flightDestinationAerodrome`" in built.cypher
    assert "flightDestinationAerodrome" in built.returned_fields
    assert built.aerodrome_columns == {"flightDestinationAerodrome": "flightDestinationAerodromeName"}


def test_filter_on_name_companion_field_is_canonicalised_to_code() -> None:
    built = build_query(
        QueryIntent(
            entity="Flight",
            fields=["name"],
            filters=[Filter(field="departureAerodromeName", op=Comparator.EQ, value="EGGD")],
        ),
        MAINTENANCE,
        STORE,
    )
    # The filter targets the real code field (guarded for the gated route category), not the
    # synthetic name companion — so the query builds instead of being denied.
    assert "n.`departureAerodrome` = $p0" in built.cypher
    assert "departureAerodromeName" not in built.cypher


# --- Cross-domain traversal (relationship-existence constraints) ------------------------

ENGINEER = principal("software_engineer")


def test_traverse_emits_nested_exists_constraint_on_anchor() -> None:
    # "Which hazard endangers the fuel system?" — anchor Hazard, OUT hop to System filtered
    # by name. The traversal is a nested EXISTS constraint; only the anchor is projected.
    built = build_query(
        QueryIntent(
            entity="Hazard",
            fields=["identifier", "criticality"],
            traverse=[
                RelationshipHop(
                    relationship="ENDANGERS", entity="System", filters=[Filter(field="name", op=Comparator.EQ, value="Fuel System")]
                )
            ],
        ),
        ENGINEER,
        STORE,
    )
    assert "MATCH (n:`Hazard`)" in built.cypher
    assert "EXISTS { MATCH (n)-[:`ENDANGERS`]->(t0:`System`)" in built.cypher
    assert "t0.`name` = $p0" in built.cypher
    # Only the anchor's fields are returned — the hop node is never projected.
    assert built.returned_fields == ["hazardIdentifier", "hazardCriticality"]
    assert "t0.`name` AS" not in built.cypher


def test_traverse_in_direction_flips_the_arrow() -> None:
    # "Which system does HAZ-FCS-001 endanger?" — anchor System, IN hop from Hazard.
    built = build_query(
        QueryIntent(
            entity="System",
            fields=["name"],
            traverse=[
                RelationshipHop(
                    relationship="ENDANGERS",
                    direction=Direction.IN,
                    entity="Hazard",
                    filters=[Filter(field="identifier", op=Comparator.EQ, value="HAZ-FCS-001")],
                )
            ],
        ),
        ENGINEER,
        STORE,
    )
    assert "EXISTS { MATCH (n)<-[:`ENDANGERS`]-(t0:`Hazard`)" in built.cypher


def test_traverse_unknown_relationship_is_denied() -> None:
    with pytest.raises(AuthorizationError, match="Relationship"):
        build_query(
            QueryIntent(entity="Hazard", traverse=[RelationshipHop(relationship="NOT_A_REL", entity="System")]),
            ENGINEER,
            STORE,
        )


def test_traverse_endpoint_mismatch_is_denied() -> None:
    # ENDANGERS connects Hazard->System, not Hazard->Component, so the hop is rejected even
    # though both Hazard and Component are granted to the engineer.
    with pytest.raises(AuthorizationError, match="Relationship"):
        build_query(
            QueryIntent(entity="Hazard", traverse=[RelationshipHop(relationship="ENDANGERS", entity="Component")]),
            ENGINEER,
            STORE,
        )


def test_traverse_to_ungranted_entity_is_denied() -> None:
    # public may not reach an engineering entity through a traversal hop (entity gate per hop).
    with pytest.raises(AuthorizationError, match="not permitted"):
        build_query(
            QueryIntent(
                entity="Aircraft",
                traverse=[RelationshipHop(relationship="ENDANGERS", direction=Direction.IN, entity="Hazard")],
            ),
            PUBLIC,
            STORE,
        )


def test_traverse_hop_filter_field_is_gated() -> None:
    # A filter on a field that is not visible on the hop entity is rejected (field gate per hop).
    with pytest.raises(AuthorizationError):
        build_query(
            QueryIntent(
                entity="Hazard",
                traverse=[
                    RelationshipHop(
                        relationship="ENDANGERS", entity="System", filters=[Filter(field="nonexistentField", op=Comparator.EQ, value="x")]
                    )
                ],
            ),
            ENGINEER,
            STORE,
        )


def test_traverse_multi_hop_nests_exists() -> None:
    # Two hops chain into nested EXISTS: WorkItem implemented-by a PullRequest that merges a
    # CodeModule named X.
    built = build_query(
        QueryIntent(
            entity="WorkItem",
            fields=["identifier"],
            traverse=[
                RelationshipHop(relationship="IMPLEMENTED_BY", entity="PullRequest"),
                RelationshipHop(relationship="MERGES", entity="CodeModule", filters=[Filter(field="name", op=Comparator.EQ, value="X")]),
            ],
        ),
        ENGINEER,
        STORE,
    )
    assert "EXISTS { MATCH (n)-[:`IMPLEMENTED_BY`]->(t0:`PullRequest`)" in built.cypher
    assert "EXISTS { MATCH (t0)-[:`MERGES`]->(t1:`CodeModule`)" in built.cypher
    assert "t1.`name` = $p0" in built.cypher


def test_traverse_applies_classification_filter_to_hop_nodes() -> None:
    # A non-gated principal gets the whole-row classification filter on every hop node, so an
    # anchor cannot be discovered through a classified connected node.
    built = build_query(
        QueryIntent(entity="Hazard", traverse=[RelationshipHop(relationship="ENDANGERS", entity="System")]),
        ENGINEER,
        STORE,
    )
    assert "t0.classification IS NULL OR t0.classification IN $__authz_classifications" in built.cypher


def test_traverse_hop_params_do_not_collide_with_anchor_filters() -> None:
    # Anchor filter takes p0; the hop filter must get a distinct param (p1), not overwrite it.
    built = build_query(
        QueryIntent(
            entity="Hazard",
            fields=["identifier"],
            filters=[Filter(field="criticality", op=Comparator.EQ, value="Catastrophic")],
            traverse=[
                RelationshipHop(
                    relationship="ENDANGERS", entity="System", filters=[Filter(field="name", op=Comparator.EQ, value="Fuel System")]
                )
            ],
        ),
        ENGINEER,
        STORE,
    )
    assert "n.`criticality` = $p0" in built.cypher
    assert "t0.`name` = $p1" in built.cypher
    assert built.parameters["p0"] == "Catastrophic"
    assert built.parameters["p1"] == "Fuel System"
