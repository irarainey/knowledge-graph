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


class QueryIntent(BaseModel):
    """A typed, validated description of what to retrieve from the graph.

    This is the surface the LLM fills in; the backend — not the LLM — turns it into Cypher.
    """

    entity: str = Field(description="The entity (node label) to query, e.g. 'Flight'.")
    fields: list[str] = Field(default_factory=list, description="Fields to return; empty means all visible fields.")
    filters: list[Filter] = Field(default_factory=list, description="Field comparisons to apply.")
    aggregate: Aggregate | None = Field(default=None, description="An optional aggregate instead of returning rows.")
    limit: int | None = Field(default=None, description="Maximum rows to return (clamped to the configured cap).")


class BuiltQuery(BaseModel):
    """The deterministic, parameterised Cypher built from an authorised intent."""

    cypher: str
    parameters: dict[str, FilterValue | list[str] | int]
    returned_fields: list[str] = Field(description="The field names the query projects (for redaction).")
    entity: str
    aggregated: bool = False
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

    def classification_predicate() -> str:
        nonlocal classification_used
        classification_used = True
        return f"(n.classification IS NULL OR n.classification IN ${_PARAM_CLASSIFICATIONS})"

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
            return_clause = f"{agg.func.value}(n.`{field}`) AS result"
        elif agg.func is AggregateFunc.COUNT:
            return_clause = "count(n) AS result"
        else:
            raise AuthorizationError(f"Aggregate '{agg.func.value}' requires a field.")
        returned_fields = ["result"]
        aggregated = True
        limit_clause = ""
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
        for f in projection:
            safe = _safe_identifier(f, "field")
            # Redact clearance-gated fields on rows above the principal's clearance (the row
            # stays visible, the protected value becomes null).
            if has_gated and store.is_field_clearance_gated(principal, entity, f):
                projected_terms.append(f"CASE WHEN {classification_predicate()} THEN n.`{safe}` ELSE null END AS `{f}`")
            else:
                projected_terms.append(f"n.`{safe}` AS `{f}`")
        return_clause = ", ".join(projected_terms)
        returned_fields = projection
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
    records: list[dict[str, object]], returned_fields: list[str], name_map: dict[str, str]
) -> list[dict[str, object]]:
    """Attach a resolved aerodrome name beside each returned aerodrome ICAO-code field.

    For every field in ``returned_fields`` that holds an aerodrome ICAO code (see
    :data:`AERODROME_CODE_FIELDS`), add a sibling ``"<field>Name"`` key resolving the code via
    ``name_map``. This lets the answer model receive both the code and the human name from a
    single retrieval instead of issuing a follow-up lookup per code. The name inherits the
    code's redaction: a code already nulled (e.g. a gated route on a classified flight) maps
    to a null name, so no redacted route ever gains a name.
    """
    code_fields = [field for field in returned_fields if field in AERODROME_CODE_FIELDS]
    if not code_fields:
        return records
    for row in records:
        for field in code_fields:
            code = row.get(field)
            row[aerodrome_name_field(field)] = name_map.get(code) if isinstance(code, str) else None
    return records
