"""Authorization primitives: the external access policy and request principals.

This package owns the authorization *trust boundary*: a selected identity is resolved
server-side, against an external versioned policy, into a :class:`Principal` carrying its
clearance and capability grants. That principal then drives enforcement — which entities,
fields and aggregates a request may touch — in the structured-intent query builder. The
package owns identity, clearance and policy versioning; the policy is data, kept outside
the graph and versioned independently of it.
"""

from __future__ import annotations

from authz.models import AccessPolicy, EntityCatalog, Identity, Principal
from authz.query_builder import (
    AERODROME_CODE_FIELDS,
    EVENT_DATED_ENTITIES,
    VERSIONED_ENTITIES,
    Aggregate,
    AggregateFunc,
    AuthorizationError,
    BuiltQuery,
    Comparator,
    Direction,
    Filter,
    QueryIntent,
    RelationshipHop,
    aerodrome_name_field,
    aggregate_alias,
    attach_aerodrome_names,
    build_query,
    output_alias,
    redact_records,
)
from authz.store import ENV_ACCESS_POLICY_PATH, PolicyError, PolicyStore

__all__ = [
    "AERODROME_CODE_FIELDS",
    "ENV_ACCESS_POLICY_PATH",
    "EVENT_DATED_ENTITIES",
    "VERSIONED_ENTITIES",
    "AccessPolicy",
    "Aggregate",
    "AggregateFunc",
    "AuthorizationError",
    "BuiltQuery",
    "Comparator",
    "Direction",
    "EntityCatalog",
    "Filter",
    "Identity",
    "PolicyError",
    "PolicyStore",
    "Principal",
    "QueryIntent",
    "RelationshipHop",
    "aerodrome_name_field",
    "aggregate_alias",
    "attach_aerodrome_names",
    "build_query",
    "output_alias",
    "redact_records",
]
