"""Phase 0 exit criterion: the domain models validate the fixture set.

Valid fixtures model the worked example (§2.1) and the response contract (§11.2);
each invalid fixture breaks exactly one documented invariant and must be rejected.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from constraintlens.domain import models
from tests.conftest import load_fixture_cases

VALID = load_fixture_cases("valid")
INVALID = load_fixture_cases("invalid")


def _model(name: str) -> type[BaseModel]:
    cls = getattr(models, name)
    assert issubclass(cls, BaseModel)
    return cls


def test_fixture_sets_are_not_empty() -> None:
    assert len(VALID) >= 10
    assert len(INVALID) >= 20


@pytest.mark.parametrize(
    ("model_name", "body"),
    [(m, b) for m, _, b in VALID],
    ids=[f"{m}/{c}" for m, c, _ in VALID],
)
def test_valid_fixture_parses_and_round_trips(model_name: str, body: dict) -> None:
    cls = _model(model_name)
    obj = cls.model_validate(body)
    again = cls.model_validate_json(obj.model_dump_json())
    assert again == obj


@pytest.mark.parametrize(
    ("model_name", "body"),
    [(m, b) for m, _, b in INVALID],
    ids=[f"{m}/{c}" for m, c, _ in INVALID],
)
def test_invalid_fixture_is_rejected(model_name: str, body: dict) -> None:
    with pytest.raises(ValidationError):
        _model(model_name).model_validate(body)
