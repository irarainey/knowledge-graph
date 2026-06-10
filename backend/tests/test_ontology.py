"""Tests for ontology metadata loading and description (Area 2 ontology versioning)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common.ontology import OntologyMeta


def test_bundled_ontology_loads() -> None:
    meta = OntologyMeta.load()
    assert meta.version
    assert meta.version != "unknown"


def test_missing_file_falls_back_to_unknown(tmp_path: Path) -> None:
    meta = OntologyMeta.load(tmp_path / "does-not-exist.json")
    assert meta.version == "unknown"
    assert meta.deprecated == []


def test_describe_includes_version_and_deprecations(tmp_path: Path) -> None:
    path = tmp_path / "ontology.json"
    path.write_text(
        json.dumps(
            {
                "version": "2.0.0",
                "deprecated": [{"term": "Spec.oldField", "supersededBy": "newField", "since": "2.0.0"}],
            }
        ),
        encoding="utf-8",
    )
    meta = OntologyMeta.load(path)
    described = meta.describe()
    assert "Ontology version: 2.0.0." in described
    assert "Spec.oldField -> newField (since v2.0.0)" in described


def test_describe_without_deprecations_is_version_only(tmp_path: Path) -> None:
    path = tmp_path / "ontology.json"
    path.write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
    meta = OntologyMeta.load(path)
    assert meta.describe() == "Ontology version: 1.0.0."


def test_env_override_is_respected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "custom.json"
    path.write_text(json.dumps({"version": "9.9.9"}), encoding="utf-8")
    monkeypatch.setenv("ONTOLOGY_PATH", str(path))
    assert OntologyMeta.load().version == "9.9.9"
