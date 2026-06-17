"""Loading and querying the external access policy (the authorization trust boundary).

:class:`PolicyStore` loads the versioned ``access-policy.json``, validates it, and turns
a client-supplied identity id into a request-scoped :class:`~authz.models.Principal`. It
is **default-deny**: an unknown or missing id resolves to the policy's least-privilege
default identity rather than to broad access.

The store is read at application startup and held on ``app.state``; the policy itself is
data, not code, so it can be re-versioned without redeploying the service.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from authz.models import AccessPolicy, EntityCatalog, Identity, Principal, RelationshipCatalogEntry
from common.logging_config import get_logger

logger = get_logger(__name__)

# Env var to override where the policy is loaded from; otherwise the bundled
# ``backend/policy/access-policy.json`` is used.
ENV_ACCESS_POLICY_PATH = "ACCESS_POLICY_PATH"

# backend/src/authz/store.py -> parents[2] == backend/, so the default policy lives at
# backend/policy/access-policy.json regardless of the process working directory.
_DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "policy" / "access-policy.json"


class PolicyError(RuntimeError):
    """Raised when the access policy is missing or fails validation."""


class PolicyStore:
    """Holds the validated access policy and resolves identities into principals."""

    def __init__(self, policy: AccessPolicy) -> None:
        self._validate(policy)
        self._policy = policy
        self._by_id: dict[str, Identity] = {identity.id: identity for identity in policy.identities}
        self._rank: dict[str, int] = {level: index for index, level in enumerate(policy.clearanceLevels)}

    @staticmethod
    def _validate(policy: AccessPolicy) -> None:
        """Reject a policy that cannot be enforced safely (fail closed at startup)."""
        if not policy.clearanceLevels:
            raise PolicyError("Access policy defines no clearanceLevels.")
        if not policy.identities:
            raise PolicyError("Access policy defines no identities.")
        ids = [identity.id for identity in policy.identities]
        if len(ids) != len(set(ids)):
            raise PolicyError("Access policy contains duplicate identity ids.")
        if policy.defaultIdentity not in ids:
            raise PolicyError(f"defaultIdentity {policy.defaultIdentity!r} is not a defined identity.")
        categories = set(policy.sensitivityCategories)
        # Every catalog field must be gated by a declared sensitivity category, so a typo
        # cannot silently create an unreachable (or worse, ungated) field.
        for entity, catalog in policy.catalog.items():
            for field, category in catalog.fields.items():
                if category not in categories:
                    raise PolicyError(f"Catalog field {entity}.{field} has unknown category {category!r}.")
        for identity in policy.identities:
            if identity.clearance not in policy.clearanceLevels:
                raise PolicyError(f"Identity {identity.id!r} has clearance {identity.clearance!r} not in clearanceLevels.")
            for category in identity.categories:
                if category not in categories:
                    raise PolicyError(f"Identity {identity.id!r} grants unknown category {category!r}.")
            for category in identity.clearanceGatedCategories:
                if category not in categories:
                    raise PolicyError(f"Identity {identity.id!r} clearance-gates unknown category {category!r}.")
                if category in identity.categories:
                    # A category is either a full grant or a clearance-gated grant, never both
                    # (the gating would be meaningless if the category were also fully granted).
                    raise PolicyError(f"Identity {identity.id!r} grants category {category!r} both fully and clearance-gated.")
            for entity in identity.entities:
                if entity not in policy.catalog:
                    raise PolicyError(f"Identity {identity.id!r} grants unknown entity {entity!r}.")
        # Every relationship-catalog endpoint must reference a catalog entity, so a traversal
        # hop can never be validated against a label that has no queryable surface.
        for rel, entry in policy.relationshipCatalog.items():
            if not entry.endpoints:
                raise PolicyError(f"Relationship {rel!r} declares no endpoints.")
            for endpoint in entry.endpoints:
                if endpoint.from_ not in policy.catalog:
                    raise PolicyError(f"Relationship {rel!r} endpoint 'from' {endpoint.from_!r} is not a catalog entity.")
                if endpoint.to not in policy.catalog:
                    raise PolicyError(f"Relationship {rel!r} endpoint 'to' {endpoint.to!r} is not a catalog entity.")

    @classmethod
    def load(cls, path: str | Path | None = None) -> PolicyStore:
        """Load and validate the policy from ``path`` (or the env override / bundled default)."""
        resolved = Path(path) if path is not None else Path(os.environ.get(ENV_ACCESS_POLICY_PATH, _DEFAULT_POLICY_PATH))
        logger.info("Loading access policy from %s", resolved)
        try:
            raw = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            raise PolicyError(f"Could not read access policy at {resolved}: {exc}") from exc
        try:
            policy = AccessPolicy.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            raise PolicyError(f"Access policy at {resolved} is invalid: {exc}") from exc
        store = cls(policy)
        logger.info("Access policy v%s loaded (%d identities, default=%s)", policy.version, len(policy.identities), policy.defaultIdentity)
        return store

    @property
    def version(self) -> str:
        return self._policy.version

    def list_identities(self) -> list[Identity]:
        """Return the selectable identities, in policy order, for the UI selector."""
        return list(self._policy.identities)

    def resolve_principal(self, identity_id: str | None) -> Principal:
        """Resolve a client-supplied identity id into a request-scoped principal.

        Default-deny: an unknown or missing id falls back to the policy's least-privilege
        ``defaultIdentity`` rather than granting broad access.
        """
        identity = self._by_id.get(identity_id or "")
        if identity is None:
            if identity_id:
                logger.warning("Unknown identity %r requested; falling back to default %r", identity_id, self._policy.defaultIdentity)
            identity = self._by_id[self._policy.defaultIdentity]
        return Principal(
            id=identity.id,
            displayName=identity.displayName,
            role=identity.role,
            clearance=identity.clearance,
            clearanceRank=self._rank[identity.clearance],
            categories=frozenset(identity.categories),
            gatedCategories=frozenset(identity.clearanceGatedCategories),
            entities=frozenset(identity.entities),
            allowAggregates=identity.allowAggregates,
            policyVersion=self._policy.version,
        )

    # --- Query-builder support ----------------------------------------------------------
    # These read-only helpers let the structured-intent query builder validate a typed
    # query against policy deterministically. They never mutate state.

    def clearance_rank(self, clearance: str | None) -> int:
        """Rank of a node's ``classification`` (unknown/missing == least sensitive)."""
        return self._rank.get(clearance or "", 0)

    def allowed_classifications(self, principal: Principal) -> list[str]:
        """Classification values a principal may see: all levels up to and including its clearance."""
        return self._policy.clearanceLevels[: principal.clearanceRank + 1]

    def entity_catalog(self, entity: str) -> EntityCatalog | None:
        """The catalog entry for an entity label, or ``None`` if it is not queryable."""
        return self._policy.catalog.get(entity)

    def relationship_catalog(self, relationship: str) -> RelationshipCatalogEntry | None:
        """The catalog entry for a relationship type, or ``None`` if it is not traversable."""
        return self._policy.relationshipCatalog.get(relationship)

    def is_relationship_permitted(self, principal: Principal, from_entity: str, relationship: str, to_entity: str) -> bool:
        """True if ``principal`` may traverse ``(from_entity)-[relationship]->(to_entity)``.

        A hop is permitted only when the relationship type exists in the catalog with a
        matching ``(from, to)`` endpoint pair AND the principal is granted *both* endpoint
        entities. Relationships carry no separate grant: they are gated by the entities they
        connect, so a principal that cannot query either end can never traverse the edge (its
        existence stays hidden).
        """
        entry = self._policy.relationshipCatalog.get(relationship)
        if entry is None:
            return False
        if from_entity not in principal.entities or to_entity not in principal.entities:
            return False
        return any(endpoint.from_ == from_entity and endpoint.to == to_entity for endpoint in entry.endpoints)

    def visible_relationships(self, principal: Principal) -> list[tuple[str, str, str, str]]:
        """The relationship hops ``principal`` may traverse, as ``(from, rel, to, description)``.

        Only endpoints whose *both* ends are visible entities (granted, with at least one
        visible field) are listed, so a relationship a principal cannot reach is never even
        named to it. Returned in policy order for stable prompts.
        """
        visible = set(self.visible_entities(principal))
        hops: list[tuple[str, str, str, str]] = []
        for relationship, entry in self._policy.relationshipCatalog.items():
            for endpoint in entry.endpoints:
                if endpoint.from_ in visible and endpoint.to in visible:
                    hops.append((endpoint.from_, relationship, endpoint.to, entry.description))
        return hops

    def field_category(self, entity: str, field: str) -> str | None:
        """The sensitivity category gating a field, or ``None`` if it is not in the catalog."""
        catalog = self._policy.catalog.get(entity)
        if catalog is None:
            return None
        return catalog.fields.get(field)

    def is_field_visible(self, principal: Principal, entity: str, field: str) -> bool:
        """True if ``principal`` may see ``entity.field`` (entity granted + category granted).

        A clearance-gated category counts as visible: the field *name* is permitted and the
        field may be projected/filtered/aggregated; its *values* are redacted per-row by the
        query builder on rows above the principal's clearance.
        """
        if entity not in principal.entities:
            return False
        category = self.field_category(entity, field)
        return category is not None and (category in principal.categories or category in principal.gatedCategories)

    def is_field_clearance_gated(self, principal: Principal, entity: str, field: str) -> bool:
        """True if ``entity.field`` is a clearance-gated field for ``principal``.

        Clearance-gated fields are visible (the name is granted) but their values must be
        redacted on rows whose classification is above the principal's clearance, rather than
        the whole row being hidden.
        """
        category = self.field_category(entity, field)
        return category is not None and category in principal.gatedCategories

    def has_gated_categories(self, principal: Principal) -> bool:
        """True if the principal has any clearance-gated category.

        Such a principal sees classified rows (redacted) instead of having them hidden, so the
        builder applies field-level redaction rather than the whole-row classification filter.
        """
        return bool(principal.gatedCategories)

    def visible_fields(self, principal: Principal, entity: str) -> list[str]:
        """All catalog fields of ``entity`` visible to ``principal`` (in catalog order).

        Includes clearance-gated fields: their names are visible (values redacted per-row).
        """
        catalog = self._policy.catalog.get(entity)
        if catalog is None or entity not in principal.entities:
            return []
        grantable = principal.categories | principal.gatedCategories
        return [field for field, category in catalog.fields.items() if category in grantable]

    def visible_entities(self, principal: Principal) -> list[str]:
        """Catalog entities ``principal`` may query that also have at least one visible field."""
        return [entity for entity in self._policy.catalog if self.visible_fields(principal, entity)]

    def describe_surface(self, principal: Principal) -> str:
        """A compact, principal-scoped description of the queryable surface for the LLM.

        Only entities and fields the principal may actually see are listed, so unauthorised
        field *names* never reach the model (field existence is itself sensitive). This is
        enforcement layer (a) — the user-scoped schema prompt — complementing the query
        builder's deterministic checks.
        """
        lines: list[str] = []
        for entity in self.visible_entities(principal):
            catalog = self._policy.catalog[entity]
            fields = ", ".join(self.visible_fields(principal, entity))
            description = f" — {catalog.description}" if catalog.description else ""
            lines.append(f"- {entity}{description}\n  fields: {fields}")
        body = "\n".join(lines) if lines else "- (no entities are available to this identity)"
        aggregates = "permitted" if principal.allowAggregates else "NOT permitted"
        rel_lines: list[str] = []
        for from_entity, relationship, to_entity, description in self.visible_relationships(principal):
            note = f" — {description}" if description else ""
            rel_lines.append(f"- ({from_entity})-[:{relationship}]->({to_entity}){note}")
        if rel_lines:
            relationships = "\n".join(rel_lines)
            rel_section = (
                "\n\nRelationships you can traverse (use these in 'traverse' to filter an entity by what it "
                f"is connected to):\n{relationships}"
            )
        else:
            rel_section = ""
        return f"{body}\n\nAggregates (count/avg/sum/min/max): {aggregates}.{rel_section}"
