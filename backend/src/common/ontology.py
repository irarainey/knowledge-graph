"""The active ontology version (schema semantics), versioned separately from data and policy.

The knowledge graph has three things that version on different clocks:

* the **graph data** (the nodes/edges themselves — see Area 2 node versioning),
* the **access policy** (who may see what — :mod:`authz`), and
* the **ontology** (what the schema *means*: which terms exist and what they denote).

This module owns the third. It is deliberately lightweight for the PoC: a semantic version
plus a list of **deprecated terms** kept under a *deprecate-don't-delete* policy, so an old
term stays interpretable even after the data has moved to its replacement. The active
version and any deprecations are surfaced to the answer model (retrieval context) and in the
debug ``stats`` event, so every answer can be attributed to the ontology it was grounded in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from common.logging_config import get_logger

logger = get_logger(__name__)

# Env override for the ontology metadata file; otherwise the bundled default is used.
ENV_ONTOLOGY_PATH = "ONTOLOGY_PATH"

# backend/src/common/ontology.py -> parents[2] == backend/, so the default lives at
# backend/policy/ontology.json regardless of the process working directory.
_DEFAULT_ONTOLOGY_PATH = Path(__file__).resolve().parents[2] / "policy" / "ontology.json"


class DeprecatedTerm(BaseModel):
    """An ontology term retained for compatibility but superseded by a newer one."""

    term: str = Field(description="The deprecated term, as 'Entity.field'.")
    supersededBy: str = Field(description="The term that replaces it.")
    since: str = Field(default="", description="Ontology version the deprecation took effect.")
    note: str = Field(default="", description="Why it was deprecated (informational).")


class OntologyMeta(BaseModel):
    """The active ontology version plus its retained deprecations."""

    version: str = Field(description="Semantic version of the active ontology.")
    description: str = Field(default="")
    deprecated: list[DeprecatedTerm] = Field(default_factory=list, description="Deprecated-but-retained terms.")

    @classmethod
    def load(cls, path: str | Path | None = None) -> OntologyMeta:
        """Load the ontology metadata from ``path`` (or the env override / bundled default).

        Falls back to a single ``unknown`` version rather than failing the service: the
        ontology version is informational (it is recorded and shown, not enforced), so a
        missing file should not take ``/ask`` down.
        """
        resolved = Path(path) if path is not None else Path(os.environ.get(ENV_ONTOLOGY_PATH, _DEFAULT_ONTOLOGY_PATH))
        try:
            raw = resolved.read_text(encoding="utf-8")
        except OSError:
            logger.warning("No ontology metadata at %s; reporting version as 'unknown'.", resolved)
            return cls(version="unknown")
        meta = cls.model_validate(json.loads(raw))
        logger.info("Ontology v%s loaded (%d deprecated term(s))", meta.version, len(meta.deprecated))
        return meta

    def describe(self) -> str:
        """A compact description of the active ontology for the answer model's context."""
        lines = [f"Ontology version: {self.version}."]
        if self.deprecated:
            terms = "; ".join(f"{d.term} -> {d.supersededBy} (since v{d.since})" for d in self.deprecated)
            lines.append(f"Deprecated terms (retained, prefer the replacement): {terms}.")
        return "\n".join(lines)
