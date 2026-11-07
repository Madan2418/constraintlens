from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture_cases(kind: str) -> list[tuple[str, str, dict[str, Any]]]:
    """``(model_name, case_name, body)`` for every fixture under ``fixtures/<kind>/<Model>/``."""
    cases = []
    for path in sorted((FIXTURES / kind).glob("*/*.json")):
        cases.append((path.parent.name, path.stem, json.loads(path.read_text(encoding="utf-8"))))
    return cases
