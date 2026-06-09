"""Unit tests for the external access policy store (authorization trust boundary)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from authz import AccessPolicy, PolicyError, PolicyStore

_VALID = {
    "version": "test.1",
    "clearanceLevels": ["unclassified", "official", "secret"],
    "defaultIdentity": "public",
    "identities": [
        {"id": "public", "displayName": "Public", "role": "public", "clearance": "unclassified"},
        {"id": "ops", "displayName": "Operations", "role": "operations", "clearance": "secret"},
    ],
}


def _store(data: dict) -> PolicyStore:
    return PolicyStore(AccessPolicy.model_validate(data))


def test_bundled_policy_loads_and_validates() -> None:
    store = PolicyStore.load()
    assert store.version
    ids = [identity.id for identity in store.list_identities()]
    assert "public" in ids


def test_resolve_known_identity_carries_clearance_rank() -> None:
    principal = _store(_VALID).resolve_principal("ops")
    assert principal.id == "ops"
    assert principal.clearance == "secret"
    assert principal.clearanceRank == 2
    assert principal.policyVersion == "test.1"


def test_resolve_unknown_identity_defaults_to_least_privilege() -> None:
    store = _store(_VALID)
    for missing in (None, "", "ghost"):
        principal = store.resolve_principal(missing)
        assert principal.id == "public"
        assert principal.clearanceRank == 0


def test_load_from_explicit_path(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(_VALID), encoding="utf-8")
    assert PolicyStore.load(path).version == "test.1"


def test_load_missing_file_raises_policy_error(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        PolicyStore.load(tmp_path / "does-not-exist.json")


def test_default_identity_must_exist() -> None:
    bad = {**_VALID, "defaultIdentity": "nobody"}
    with pytest.raises(PolicyError, match="defaultIdentity"):
        _store(bad)


def test_identity_clearance_must_be_a_known_level() -> None:
    bad = {
        **_VALID,
        "identities": [{"id": "public", "displayName": "Public", "role": "public", "clearance": "cosmic"}],
    }
    with pytest.raises(PolicyError, match="clearance"):
        _store(bad)


def test_duplicate_identity_ids_rejected() -> None:
    bad = {
        **_VALID,
        "identities": [
            {"id": "dup", "displayName": "A", "role": "public", "clearance": "unclassified"},
            {"id": "dup", "displayName": "B", "role": "public", "clearance": "unclassified"},
        ],
        "defaultIdentity": "dup",
    }
    with pytest.raises(PolicyError, match="duplicate"):
        _store(bad)
