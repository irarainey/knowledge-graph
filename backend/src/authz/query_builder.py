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
4. **Row-level classification filter** — a clearance filter is injected into the ``WHERE``
   clause so classified nodes (e.g. military flights) never participate in the query for an
   under-cleared principal — not even in an aggregate or an existence check.

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


_PARAM_CLASSIFICATIONS = "__authz_classifications"
_PARAM_LIMIT = "__authz_limit"


def _safe_identifier(name: str, kind: str) -> str:
    if not isinstance(name, str) or not _VALID_IDENTIFIER.match(name):
        raise AuthorizationError(f"Unsafe {kind} name.")
    return name


def _require_visible(store: PolicyStore, principal: Principal, entity: str, field: str) -> None:
    if not store.is_field_visible(principal, entity, field):
        # Generic message: does not reveal whether the field exists or is merely hidden.
        raise AuthorizationError(f"Field '{field}' on '{entity}' is not permitted for this identity.")


def build_query(intent: QueryIntent, principal: Principal, store: PolicyStore) -> BuiltQuery:
    """Validate ``intent`` against policy and build parameterised, read-only Cypher.

    Raises :class:`AuthorizationError` if the entity, any field, any filter field, or the
    aggregate is not permitted for ``principal``. The returned query always carries a
    clearance filter so classified rows cannot participate in execution.
    """
    entity = intent.entity
    if store.entity_catalog(entity) is None or entity not in principal.entities:
        raise AuthorizationError(f"Entity '{entity}' is not permitted for this identity.")
    label = _safe_identifier(entity, "entity")

    parameters: dict[str, FilterValue | list[str] | int] = {}

    # --- WHERE: row-level classification filter (always) + caller filters ---------------
    # Classified nodes never participate in execution for an under-cleared principal — not
    # even inside an aggregate or existence check.
    allowed = store.allowed_classifications(principal)
    parameters[_PARAM_CLASSIFICATIONS] = allowed
    where = [f"(n.classification IS NULL OR n.classification IN ${_PARAM_CLASSIFICATIONS})"]

    for index, flt in enumerate(intent.filters):
        _require_visible(store, principal, entity, flt.field)
        field = _safe_identifier(flt.field, "field")
        param = f"p{index}"
        parameters[param] = flt.value
        where.append(f"n.`{field}` {flt.op.value} ${param}")

    where_clause = " AND ".join(where)

    # --- RETURN: aggregate (gated) or projected fields ----------------------------------
    if intent.aggregate is not None:
        agg = intent.aggregate
        if not principal.allowAggregates:
            raise AuthorizationError("Aggregate queries are not permitted for this identity.")
        if agg.field is not None:
            _require_visible(store, principal, entity, agg.field)
            field = _safe_identifier(agg.field, "field")
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
            for field_name in intent.fields:
                _require_visible(store, principal, entity, field_name)
            projection = list(dict.fromkeys(intent.fields))
        else:
            projection = store.visible_fields(principal, entity)
        if not projection:
            raise AuthorizationError(f"No visible fields on '{entity}' for this identity.")
        return_clause = ", ".join(f"n.`{_safe_identifier(f, 'field')}` AS `{f}`" for f in projection)
        returned_fields = projection
        aggregated = False
        cap = row_cap()
        limit = cap if intent.limit is None else max(1, min(intent.limit, cap))
        parameters[_PARAM_LIMIT] = limit
        limit_clause = f" LIMIT ${_PARAM_LIMIT}"

    cypher = f"MATCH (n:`{label}`) WHERE {where_clause} RETURN {return_clause}{limit_clause}"
    # Defence-in-depth: the builder only emits read-only MATCH/RETURN, but re-check anyway.
    assert_safe_cypher(cypher)
    return BuiltQuery(cypher=cypher, parameters=parameters, returned_fields=returned_fields, entity=entity, aggregated=aggregated)


def redact_records(records: list[dict[str, object]], returned_fields: list[str]) -> list[dict[str, object]]:
    """Drop any keys not in ``returned_fields`` (defence-in-depth after retrieval).

    The builder only ever projects authorised fields, so this is a safety net: it guarantees
    that even if a row carried an unexpected key (e.g. ``classification`` itself), it is
    stripped before the rows reach the answer LLM.
    """
    allowed = set(returned_fields)
    return [{key: value for key, value in row.items() if key in allowed} for row in records]
