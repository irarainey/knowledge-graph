"""Tests for the /ask request model, focused on as_of validation (Area 2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from models import AskRequest


def test_as_of_defaults_to_none() -> None:
    req = AskRequest(question="How fast does it cruise?")
    assert req.as_of is None


def test_valid_iso_date_is_accepted() -> None:
    req = AskRequest(question="q", as_of="2021-06-01")
    assert req.as_of == "2021-06-01"


@pytest.mark.parametrize("bad", ["2021/06/01", "01-06-2021", "2021-6-1", "yesterday", "2021-06-01T00:00:00"])
def test_malformed_as_of_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        AskRequest(question="q", as_of=bad)
