"""``InMemoryJudgeCache``: JudgeCache fake (§6.3, §7.4). SQLite lives in ``store/sqlite.py``."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from constraintlens.domain.models import JudgeResult


class InMemoryJudgeCache:
    def __init__(self) -> None:
        self._rows: dict[str, tuple[JudgeResult, str, str]] = {}

    async def get(self, key: str) -> JudgeResult | None:
        row = self._rows.get(key)
        return row[0] if row else None

    async def put(self, key: str, result: JudgeResult, *, model: str, prompt_version: str) -> None:
        self._rows.setdefault(key, (result, model, prompt_version))  # first answer wins

    def __len__(self) -> int:
        return len(self._rows)
