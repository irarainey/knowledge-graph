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

from authz.models import AccessPolicy, EntityCatalog, Identity, Principal
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
            for entity in identity.entities:
                if entity not in policy.catalog:
                    raise PolicyError(f"Identity {identity.id!r} grants unknown entity {entity!r}.")

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

    def field_category(self, entity: str, field: str) -> str | None:
        """The sensitivity category gating a field, or ``None`` if it is not in the catalog."""
        catalog = self._policy.catalog.get(entity)
        if catalog is None:
            return None
        return catalog.fields.get(field)

    def is_field_visible(self, principal: Principal, entity: str, field: str) -> bool:
        """True if ``principal`` may see ``entity.field`` (entity granted + category granted)."""
        if entity not in principal.entities:
            return False
        category = self.field_category(entity, field)
        return category is not None and category in principal.categories

    def visible_fields(self, principal: Principal, entity: str) -> list[str]:
        """All catalog fields of ``entity`` visible to ``principal`` (in catalog order)."""
        catalog = self._policy.catalog.get(entity)
        if catalog is None or entity not in principal.entities:
            return []
        return [field for field, category in catalog.fields.items() if category in principal.categories]

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
        return f"{body}\n\nAggregates (count/avg/sum/min/max): {aggregates}."
