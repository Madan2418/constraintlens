from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture_cases(kind: str) -> list[tuple[str, str, dict[str, Any]]]:
    """``(model_name, case_name, body)`` for every fixture under ``fixtures/<kind>/<Model>/``."""
    cases = []
    for path in sorted((FIXTURES / kind).glob("*/*.json")):
        cases.append((path.parent.name, path.stem, json.loads(path.read_text(encoding="utf-8"))))
    return cases


@pytest.fixture(autouse=True)
async def _close_service_stores() -> AsyncIterator[None]:
    """Close SQLite stores that ``build_app`` opened for a test (no leaked connections)."""
    yield
    from tests.service_helpers import OPEN_STORES

    while OPEN_STORES:
        await OPEN_STORES.pop().close()
