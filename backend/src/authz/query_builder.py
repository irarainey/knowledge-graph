"""Policy-aware structured-intent query builder.

This is the authorization **enforcement** layer. Instead of letting an LLM emit arbitrary
Cypher, the agent emits a typed :class:`QueryIntent` (entity + filters + fields + optional
aggregate). This module **validates that intent against the principal's policy** and then
**deterministically builds a parameterised, read-only Cypher query** — no LLM is involved
in turning intent into Cypher.

Why this is secure where free-form Text2Cypher + redaction is not: unauthorised data never
participates in execution. Enforcement is layered and deterministic, all outside the LLM:

1. **Entity gate** — the entity must be in the principal's granted entities and the catalog.
2. **Field gate** — every projected *and* filtered field must be visible to the principal
   (its sensitivity category must be granted). Filtering on a hidden field leaks its values,
   so filters are gated too.
3. **Aggregate gate** — aggregates require an explicit grant and a visible target field;
   this closes the COUNT/AVG/existence inference channel structurally.
4. **Row-level classification filter** — by default a clearance filter is injected into the
   ``WHERE`` clause so classified nodes (e.g. military flights) never participate in the query
   for an under-cleared principal — not even in an aggregate or an existence check.
5. **Clearance-gated categories (opt-in per identity)** — an identity may instead mark a
   category as clearance-gated: classified rows then stay visible but that category's fields
   are protected field-by-field rather than the whole row being hidden. The gated fields are
   nulled on out-of-clearance rows in the projection, gated-field filters cannot match those
   rows, and gated-field aggregates exclude them — while non-gated fields and counts still see
   the rows. This lets e.g. maintenance see that a classified flight existed and include its
   hours in totals without ever exposing its route. It is a deliberate, auditable relaxation
   (it reopens an existence/aggregate inference channel for the gated rows), so it is off by
   default and granted only to identities whose need-to-know requires it.

Values are always parameterised; labels and field names (which Cypher cannot parameterise)
are taken only from the controlled catalog and additionally validated against a strict
identifier pattern before interpolation. The built query is finally re-checked by
:func:`common.query_safety.assert_safe_cypher` as defence-in-depth.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from authz.models import Principal
from authz.store import PolicyStore
from common.query_safety import assert_safe_cypher, row_cap

# Labels and field names are interpolated into Cypher (it cannot parameterise them), so they
# must match a strict identifier pattern. Catalog keys are controlled by us, but we validate
# anyway as defence-in-depth against a malformed policy.
_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AuthorizationError(Exception):
    """Raised when a structured query intent is not permitted for the principal.

    The message is intentionally generic (it does not confirm whether a field exists) so a
    denial cannot itself be used to probe the hidden schema.
    """


class Comparator(StrEnum):
    """The filter comparison operators the builder will emit (a closed, safe set)."""

    EQ = "="
    NE = "<>"
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    CONTAINS = "CONTAINS"
    STARTS_WITH = "STARTS WITH"
    ENDS_WITH = "ENDS WITH"


class AggregateFunc(StrEnum):
    """The aggregate functions the builder will emit (a closed, safe set)."""

    COUNT = "count"
    AVG = "avg"
    SUM = "sum"
    MIN = "min"
    MAX = "max"


FilterValue = str | int | float | bool


class Filter(BaseModel):
    """A single field comparison applied to the entity."""

    field: str = Field(description="The entity field to filter on (must be visible to the principal).")
    op: Comparator = Field(description="The comparison operator.")
    value: FilterValue = Field(description="The value to compare against (always parameterised).")


class Aggregate(BaseModel):
    """An aggregate over the matched rows. ``field`` is omitted for a plain ``count``."""

    func: AggregateFunc = Field(description="The aggregate function to apply.")
    field: str | None = Field(default=None, description="The field to aggregate; omit for count of rows.")


class Direction(StrEnum):
    """The direction a traversal hop follows a relationship, relative to the prior node."""

    OUT = "out"
    IN = "in"


class RelationshipHop(BaseModel):
    """One hop of a traversal: follow ``relationship`` to a connected ``entity``.

    A hop is a *constraint* on the anchor entity, not a projection: it requires the matched
    anchor (or the previous hop's node) to be connected, via ``relationship`` in ``direction``,
    to a node of ``entity`` that satisfies the hop's ``filters``. Hops chain to express
    multi-step paths. The query still returns only the anchor entity's fields.
    """

    relationship: str = Field(description="The relationship type to follow, e.g. 'ENDANGERS' (must be in the catalog).")
    entity: str = Field(description="The connected entity (node label) at the far end of this hop.")
    direction: Direction = Field(
        default=Direction.OUT,
        description="'out' follows (prev)-[:REL]->(entity); 'in' follows (prev)<-[:REL]-(entity).",
    )
    filters: list[Filter] = Field(default_factory=list, description="Optional field comparisons applied to the hop's entity.")


class QueryIntent(BaseModel):
    """A typed, validated description of what to retrieve from the graph.

    This is the surface the LLM fills in; the backend — not the LLM — turns it into Cypher.
    """

    entity: str = Field(description="The entity (node label) to query, e.g. 'Flight'.")
    fields: list[str] = Field(default_factory=list, description="Fields to return; empty means all visible fields.")
    filters: list[Filter] = Field(default_factory=list, description="Field comparisons to apply.")
    aggregate: Aggregate | None = Field(default=None, description="An optional aggregate instead of returning rows.")
    limit: int | None = Field(default=None, description="Maximum rows to return (clamped to the configured cap).")
    traverse: list[RelationshipHop] = Field(
        default_factory=list,
        description="Optional chain of relationship hops constraining the entity by what it is connected to.",
    )


class BuiltQuery(BaseModel):
    """The deterministic, parameterised Cypher built from an authorised intent."""

    cypher: str
    parameters: dict[str, FilterValue | list[str] | int]
    returned_fields: list[str] = Field(description="The aliased output columns the query projects (for redaction).")
    entity: str
    aggregated: bool = False
    aerodrome_columns: dict[str, str] = Field(
        default_factory=dict,
        description="Map of aerodrome code column alias -> companion name column alias, for name attachment.",
    )
    versioned: bool = Field(default=False, description="True if the entity is temporally versioned (a temporal filter was injected).")
    version_mode: str = Field(default="current", description="Temporal mode applied: 'current', or 'as-of' when a date was supplied.")
    as_of: str | None = Field(default=None, description="The as-of date the temporal filter used, if any.")
    event_dated: bool = Field(
        default=False, description="True if an event-date cutoff was injected (an as-of query over an event-dated entity)."
    )

    @property
    def temporal_filter_applied(self) -> bool:
        """Whether any temporal predicate (version selection or event-date cutoff) was injected."""
        return self.versioned or self.event_dated


_PARAM_CLASSIFICATIONS = "__authz_classifications"
_PARAM_LIMIT = "__authz_limit"
_PARAM_AS_OF = "__asOf"

# Entities whose nodes are temporally versioned (carry logicalId/version/validFrom/validTo/
# current). Only these get a temporal predicate injected; everything else is queried as-is.
# Kept deliberately narrow for the PoC — versioning the whole graph would force the model to
# reason about validity windows it cannot see, mixing current and historical facts. The set
# is structural ontology metadata, not authorization, so it lives here next to the builder
# rather than in the access policy.
VERSIONED_ENTITIES: frozenset[str] = frozenset({"Specification"})

# Entities that are *event-dated* rather than versioned: immutable events carrying an ISO
# ``date`` (e.g. Flight). They are never versioned — an event is not a "version" of anything —
# but under an as-of snapshot only events that had already occurred are included, so a
# historical view reflects the graph as it existed on that date. The cutoff is injected
# deterministically, only for these entities and only when an as_of date is supplied.
EVENT_DATED_ENTITIES: frozenset[str] = frozenset({"Flight"})
_EVENT_DATE_FIELD = "date"

# Fields whose value is an aerodrome ICAO code (e.g. "EGGD"). When one of these is returned,
# the backend resolves the code to the aerodrome's human name server-side and attaches it as
# "<field>Name" (see :func:`attach_aerodrome_names`), so the answer model receives both the
# code and the name from a single retrieval instead of issuing a follow-up lookup per code.
AERODROME_CODE_FIELDS: frozenset[str] = frozenset({"departureAerodrome", "destinationAerodrome"})


def output_alias(entity: str, field: str) -> str:
    """The deterministic, entity-qualified camelCase column alias a field is projected under.

    Output columns are aliased ``<entityCamel><FieldPascal>`` (e.g. ``(Flight, distance_nm)``
    → ``flightDistance_nm``; ``(PistonEngine, ratedHorsepower)`` → ``pistonEngineRatedHorsepower``)
    so every returned column is globally unique and self-describing: a consumer (or the
    evaluation harness) can tell which entity and field a value belongs to from the column name
    alone, instead of relying on bare field names that collide across entities. This is an
    output-side concern only — the LLM-facing intent vocabulary stays in catalog-field-name space.
    """
    entity_camel = entity[0].lower() + entity[1:] if entity else entity
    field_pascal = field[0].upper() + field[1:] if field else field
    return f"{entity_camel}{field_pascal}"


def aggregate_alias(entity: str) -> str:
    """The entity-qualified alias an aggregate result is projected under, e.g. ``flightResult``."""
    return output_alias(entity, "result")


def aerodrome_name_field(code_field: str) -> str:
    """The companion key that carries the resolved name for an aerodrome code field."""
    return f"{code_field}Name"


# Reverse map: the synthetic "<code>Name" companion key back to its real code field. The
# answer model is told the name appears in results automatically, but it sometimes still
# references the companion key in its intent; canonicalising it to the underlying code field
# (rather than rejecting the whole query as an unknown field) keeps such queries working — the
# name is re-attached after retrieval by :func:`attach_aerodrome_names`.
_AERODROME_NAME_TO_CODE: dict[str, str] = {aerodrome_name_field(code): code for code in AERODROME_CODE_FIELDS}


def _canonical_field(field: str) -> str:
    """Map an aerodrome name-companion field back to its real code field; pass others through."""
    return _AERODROME_NAME_TO_CODE.get(field, field)


def _safe_identifier(name: str, kind: str) -> str:
    if not isinstance(name, str) or not _VALID_IDENTIFIER.match(name):
        raise AuthorizationError(f"Unsafe {kind} name.")
    return name


def _require_visible(store: PolicyStore, principal: Principal, entity: str, field: str) -> None:
    if not store.is_field_visible(principal, entity, field):
        # Generic message: does not reveal whether the field exists or is merely hidden.
        raise AuthorizationError(f"Field '{field}' on '{entity}' is not permitted for this identity.")


def build_query(intent: QueryIntent, principal: Principal, store: PolicyStore, *, as_of: str | None = None) -> BuiltQuery:
    """Validate ``intent`` against policy and build parameterised, read-only Cypher.

    Raises :class:`AuthorizationError` if the entity, any field, any filter field, or the
    aggregate is not permitted for ``principal``. By default the returned query carries a
    clearance filter so classified rows cannot participate in execution; for a principal with
    clearance-gated categories that whole-row filter is replaced by per-field redaction (see
    the module docstring) so such rows stay visible with only the gated fields protected.

    Temporal filtering is applied here too, deterministically (never by the LLM), in two
    distinct flavours:

    * **Versioning** (valid-time) — if the entity is in :data:`VERSIONED_ENTITIES`, a
      predicate is injected: with no ``as_of`` only the *current* version of each logical
      node participates; with an ``as_of`` date only the version valid at that date does.
    * **Event-date cutoff** (event-time) — if the entity is in :data:`EVENT_DATED_ENTITIES`
      and an ``as_of`` date is supplied, only events that had already occurred by that date
      (``date <= as_of``) participate, so a historical view reflects the graph as it existed
      then. Events are never *versioned* — an event is not a version of anything.

    Entities in neither set are queried unchanged — a temporal predicate is never applied to
    a node type that lacks the relevant property (which would silently drop every row).
    """
    entity = intent.entity
    if store.entity_catalog(entity) is None or entity not in principal.entities:
        raise AuthorizationError(f"Entity '{entity}' is not permitted for this identity.")
    label = _safe_identifier(entity, "entity")

    parameters: dict[str, FilterValue | list[str] | int] = {}

    # --- Row-level classification handling ----------------------------------------------
    # By default classified nodes never participate in execution for an under-cleared
    # principal (a whole-row filter) — not even inside an aggregate or existence check.
    #
    # A principal with *clearance-gated* categories is the deliberate exception: such rows
    # stay visible but the gated-category fields are redacted per-row (so e.g. maintenance
    # sees that a classified flight existed and counts its hours, without its route). For
    # those principals the whole-row filter is omitted and protection is applied field-by-
    # field instead: the gated fields are nulled on out-of-clearance rows in the projection,
    # gated-field filters cannot match classified rows, and gated-field aggregates exclude
    # them. Non-gated fields (and counts) still see classified rows, which is what lets the
    # true totals (e.g. flying hours) include classified flights.
    allowed = store.allowed_classifications(principal)
    has_gated = store.has_gated_categories(principal)
    classification_used = False

    def classification_predicate(var: str = "n") -> str:
        nonlocal classification_used
        classification_used = True
        return f"({var}.classification IS NULL OR {var}.classification IN ${_PARAM_CLASSIFICATIONS})"

    where: list[str] = []
    if not has_gated:
        where.append(classification_predicate())

    # --- WHERE: temporal version filter (only for versioned entities) -------------------
    versioned = entity in VERSIONED_ENTITIES
    version_mode = "current"
    if versioned:
        if as_of is None:
            where.append("n.current = true")
        else:
            version_mode = "as-of"
            parameters[_PARAM_AS_OF] = as_of
            where.append(f"(n.validFrom <= ${_PARAM_AS_OF} AND (n.validTo IS NULL OR ${_PARAM_AS_OF} < n.validTo))")

    # --- WHERE: event-date cutoff (only for event-dated entities, only as-of) ------------
    # An event that had not yet occurred on the as-of date is not in that historical snapshot.
    event_dated = False
    if entity in EVENT_DATED_ENTITIES and as_of is not None:
        event_dated = True
        parameters[_PARAM_AS_OF] = as_of
        where.append(f"n.`{_EVENT_DATE_FIELD}` <= ${_PARAM_AS_OF}")

    for index, flt in enumerate(intent.filters):
        flt_field = _canonical_field(flt.field)
        _require_visible(store, principal, entity, flt_field)
        field = _safe_identifier(flt_field, "field")
        param = f"p{index}"
        parameters[param] = flt.value
        predicate = f"n.`{field}` {flt.op.value} ${param}"
        # A filter on a clearance-gated field must not let classified rows be discovered by
        # their protected values (e.g. finding a military flight by its destination), so it
        # only matches rows within the principal's clearance.
        if has_gated and store.is_field_clearance_gated(principal, entity, flt_field):
            predicate = f"({classification_predicate()} AND {predicate})"
        where.append(predicate)

    # --- WHERE: traversal constraints (nested EXISTS path filters) -----------------------
    # A traversal hop constrains the anchor by what it is connected to, without projecting the
    # far node. Each hop is validated independently against policy — the relationship type and
    # its endpoint labels against the relationship catalog, the hop's target entity against the
    # principal's grants, and each hop filter field against field visibility — then emitted as a
    # nested EXISTS so unauthorised nodes never participate. Hops chain into nested EXISTS to
    # express multi-step paths. The traversal never widens the projection: only the anchor is
    # returned, so all existing redaction/aliasing applies unchanged.
    if intent.traverse:
        param_counter = [len(intent.filters)]

        def build_hop_exists(hops: list[RelationshipHop], parent_var: str, parent_entity: str, depth: int) -> str:
            hop = hops[0]
            var = f"t{depth}"
            rel = _safe_identifier(hop.relationship, "relationship")
            target = hop.entity
            if store.entity_catalog(target) is None or target not in principal.entities:
                raise AuthorizationError(f"Entity '{target}' is not permitted for this identity.")
            target_label = _safe_identifier(target, "entity")
            # Direction maps the (from, to) the relationship catalog is keyed on: OUT keeps the
            # prior node as 'from'; IN flips it (the edge points back from the hop node).
            if hop.direction is Direction.OUT:
                if not store.is_relationship_permitted(principal, parent_entity, hop.relationship, target):
                    raise AuthorizationError(f"Relationship '{hop.relationship}' is not permitted for this identity.")
                pattern = f"({parent_var})-[:`{rel}`]->({var}:`{target_label}`)"
            else:
                if not store.is_relationship_permitted(principal, target, hop.relationship, parent_entity):
                    raise AuthorizationError(f"Relationship '{hop.relationship}' is not permitted for this identity.")
                pattern = f"({parent_var})<-[:`{rel}`]-({var}:`{target_label}`)"
            conditions: list[str] = []
            # Mirror the anchor's whole-row classification filter on each hop node (for a
            # non-gated principal) so an anchor cannot be discovered through a classified node.
            if not has_gated:
                conditions.append(classification_predicate(var))
            for hop_flt in hop.filters:
                hop_field = _canonical_field(hop_flt.field)
                _require_visible(store, principal, target, hop_field)
                safe_field = _safe_identifier(hop_field, "field")
                param_counter[0] += 1
                param = f"p{param_counter[0] - 1}"
                parameters[param] = hop_flt.value
                predicate = f"{var}.`{safe_field}` {hop_flt.op.value} ${param}"
                if has_gated and store.is_field_clearance_gated(principal, target, hop_field):
                    predicate = f"({classification_predicate(var)} AND {predicate})"
                conditions.append(predicate)
            if len(hops) > 1:
                conditions.append(build_hop_exists(hops[1:], var, target, depth + 1))
            inner_where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
            return f"EXISTS {{ MATCH {pattern}{inner_where} }}"

        where.append(build_hop_exists(intent.traverse, "n", entity, 0))

    # --- RETURN: aggregate (gated) or projected fields ----------------------------------
    if intent.aggregate is not None:
        agg = intent.aggregate
        if not principal.allowAggregates:
            raise AuthorizationError("Aggregate queries are not permitted for this identity.")
        if agg.field is not None:
            agg_field = _canonical_field(agg.field)
            _require_visible(store, principal, entity, agg_field)
            field = _safe_identifier(agg_field, "field")
            # Aggregating a clearance-gated field must exclude rows above clearance so the
            # protected values (e.g. military routes/distance) never contribute to the result.
            if has_gated and store.is_field_clearance_gated(principal, entity, agg_field):
                where.append(classification_predicate())
            return_clause = f"{agg.func.value}(n.`{field}`) AS `{aggregate_alias(entity)}`"
        elif agg.func is AggregateFunc.COUNT:
            return_clause = f"count(n) AS `{aggregate_alias(entity)}`"
        else:
            raise AuthorizationError(f"Aggregate '{agg.func.value}' requires a field.")
        returned_fields = [aggregate_alias(entity)]
        aggregated = True
        limit_clause = ""
        aerodrome_columns: dict[str, str] = {}
    else:
        if intent.fields:
            projection = list(dict.fromkeys(_canonical_field(field_name) for field_name in intent.fields))
            for field_name in projection:
                _require_visible(store, principal, entity, field_name)
        else:
            projection = store.visible_fields(principal, entity)
        if not projection:
            raise AuthorizationError(f"No visible fields on '{entity}' for this identity.")
        projected_terms: list[str] = []
        returned_fields = []
        aerodrome_columns = {}
        for f in projection:
            safe = _safe_identifier(f, "field")
            alias = output_alias(entity, f)
            # Redact clearance-gated fields on rows above the principal's clearance (the row
            # stays visible, the protected value becomes null).
            if has_gated and store.is_field_clearance_gated(principal, entity, f):
                projected_terms.append(f"CASE WHEN {classification_predicate()} THEN n.`{safe}` ELSE null END AS `{alias}`")
            else:
                projected_terms.append(f"n.`{safe}` AS `{alias}`")
            returned_fields.append(alias)
            if f in AERODROME_CODE_FIELDS:
                aerodrome_columns[alias] = aerodrome_name_field(alias)
        return_clause = ", ".join(projected_terms)
        aggregated = False
        cap = row_cap()
        limit = cap if intent.limit is None else max(1, min(intent.limit, cap))
        parameters[_PARAM_LIMIT] = limit
        limit_clause = f" LIMIT ${_PARAM_LIMIT}"

    if classification_used:
        parameters[_PARAM_CLASSIFICATIONS] = allowed

    where_clause = " AND ".join(where)
    match_where = f" WHERE {where_clause}" if where_clause else ""
    cypher = f"MATCH (n:`{label}`){match_where} RETURN {return_clause}{limit_clause}"
    # Defence-in-depth: the builder only emits read-only MATCH/RETURN, but re-check anyway.
    assert_safe_cypher(cypher)
    return BuiltQuery(
        cypher=cypher,
        parameters=parameters,
        returned_fields=returned_fields,
        entity=entity,
        aggregated=aggregated,
        aerodrome_columns=aerodrome_columns,
        versioned=versioned,
        version_mode=version_mode,
        as_of=as_of if (versioned or event_dated) else None,
        event_dated=event_dated,
    )


def redact_records(records: list[dict[str, object]], returned_fields: list[str]) -> list[dict[str, object]]:
    """Drop any keys not in ``returned_fields`` (defence-in-depth after retrieval).

    The builder only ever projects authorised fields, so this is a safety net: it guarantees
    that even if a row carried an unexpected key (e.g. ``classification`` itself), it is
    stripped before the rows reach the answer LLM.
    """
    allowed = set(returned_fields)
    return [{key: value for key, value in row.items() if key in allowed} for row in records]


def attach_aerodrome_names(
    records: list[dict[str, object]], aerodrome_columns: dict[str, str], name_map: dict[str, str]
) -> list[dict[str, object]]:
    """Attach a resolved aerodrome name beside each returned aerodrome ICAO-code column.

    ``aerodrome_columns`` maps each aerodrome code column alias to its companion name column
    alias (see :class:`BuiltQuery.aerodrome_columns`). For every such column holding an
    aerodrome ICAO code, the companion key is populated by resolving the code via ``name_map``.
    This lets the answer model receive both the code and the human name from a single retrieval
    instead of issuing a follow-up lookup per code. The name inherits the code's redaction: a
    code already nulled (e.g. a gated route on a classified flight) maps to a null name, so no
    redacted route ever gains a name.
    """
    if not aerodrome_columns:
        return records
    for row in records:
        for code_alias, name_alias in aerodrome_columns.items():
            code = row.get(code_alias)
            row[name_alias] = name_map.get(code) if isinstance(code, str) else None
    return records
