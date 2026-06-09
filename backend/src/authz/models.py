"""Data models for the external access policy (the authorization trust boundary).

These models describe the *policy* (the versioned ``access-policy.json`` file) and the
request-scoped :class:`Principal` the backend resolves a selected identity into. They are
declarative: an identity carries its clearance plus its **capability grants** (the
sensitivity *categories* of fields it may see, the *entities* it may query, and whether it
may run aggregates). The :class:`EntityCatalog` describes the queryable surface — which
fields exist on each entity and which sensitivity category each belongs to. Together these
let the structured-intent query builder validate a typed query against policy
deterministically, outside the LLM.

Authorization has two independent dimensions:

* **Category grants (role-based)** — *what kinds of field* an identity may see (e.g.
  ``duration`` vs ``route``). Needed because two identities can share a clearance yet have
  different need-to-know (maintenance and public are both ``unclassified``).
* **Clearance (row-level)** — *which rows* an identity may see, gated against each node's
  in-graph ``classification`` (e.g. classified military flights are ``secret``).

Property names are camelCase to mirror the JSON policy file (the repo allows ``N815`` for
Pydantic models matching JSON, see ``pyproject.toml``).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Identity(BaseModel):
    """A selectable identity defined in the external access policy.

    This is both a policy entry and the public shape returned by ``GET /users`` so the
    chat UI can populate its identity selector. It carries the user's attributes and
    capability grants — never any secret — so it is safe to expose to the frontend.
    """

    id: str = Field(description="Stable identifier sent back on /ask as the acting user.")
    displayName: str = Field(description="Human-readable label shown in the UI selector.")
    role: str = Field(description="Coarse role, e.g. 'maintenance' or 'operations'.")
    clearance: str = Field(description="Clearance level; must be one of the policy's clearanceLevels.")
    categories: list[str] = Field(default_factory=list, description="Sensitivity categories of fields this identity may see.")
    entities: list[str] = Field(default_factory=list, description="Catalog entities this identity may query.")
    allowAggregates: bool = Field(default=False, description="Whether this identity may run aggregate queries (count/avg/…).")
    description: str = Field(default="", description="What this identity is allowed to see (informational).")


class EntityCatalog(BaseModel):
    """The queryable surface for one entity (node label): its fields and their categories.

    The catalog is a *curated* projection of the graph — not every raw property is
    exposed. ``fields`` maps each queryable field name to the sensitivity category that
    gates it; a field absent from the catalog is not queryable by anyone (default-deny).
    """

    description: str = Field(default="", description="What this entity represents (shown to the LLM).")
    fields: dict[str, str] = Field(description="Map of queryable field name -> sensitivity category.")


class AccessPolicy(BaseModel):
    """The full external, versioned access policy loaded from ``access-policy.json``.

    The policy is versioned independently of the graph data and the ontology so that who
    may see what can change without re-importing the graph, and so every answer can be
    attributed to the exact policy version it was authorized under.
    """

    version: str = Field(description="Opaque policy version recorded against every request for audit.")
    description: str = Field(default="")
    clearanceLevels: list[str] = Field(description="Clearance levels ordered from least to most privileged.")
    sensitivityCategories: list[str] = Field(default_factory=list, description="The sensitivity categories fields can carry.")
    defaultIdentity: str = Field(description="Identity id unknown/unselected users resolve to (default-deny).")
    catalog: dict[str, EntityCatalog] = Field(default_factory=dict, description="Queryable entities and their fields/categories.")
    identities: list[Identity] = Field(description="The identities a user may act as.")


class Principal(BaseModel):
    """The resolved acting subject for a single request.

    The backend never trusts a raw user id from the client as an authorization decision:
    it resolves the selected identity against the policy into a Principal here, at the
    trust boundary. The Principal carries the clearance *rank* (so comparisons are
    numeric, not string), its capability grants (categories, entities, aggregate
    permission), and the policy version it was resolved under — all of which the audit
    trail and the structured-intent query builder rely on.

    NOTE: This is identity *selection*, not authentication. In a real system the id would
    come from a verified token; here it comes from a UI dropdown for demonstration.
    """

    id: str
    displayName: str
    role: str
    clearance: str
    clearanceRank: int = Field(description="Index of `clearance` in the policy's ordered clearanceLevels.")
    categories: frozenset[str] = Field(default_factory=frozenset, description="Sensitivity categories this principal may see.")
    entities: frozenset[str] = Field(default_factory=frozenset, description="Catalog entities this principal may query.")
    allowAggregates: bool = Field(default=False, description="Whether this principal may run aggregate queries.")
    policyVersion: str = Field(description="Version of the policy this principal was resolved under.")
