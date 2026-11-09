"""Shared logic for real LLM judges (ARCHITECTURE.md §7.1, §7.4, §10.1).

A provider adapter only implements ``_complete(system, user, json_schema)`` and returns
the model's text. This base renders the versioned prompts and enforces the judge
contract: strict JSON ``{verdict, reason, confidence, evidence}``, one repair retry
carrying the validation error, then ``DependencyUnavailable`` (a judge failure that
Tier 2 turns into a degraded verdict).
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from constraintlens.adapters.judge.prompts import load_prompt
from constraintlens.domain.errors import DependencyUnavailable
from constraintlens.domain.models import JudgeResult

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from constraintlens.domain.models import ProposedAction

#: JSON Schema for structured output. Kept to keywords every provider supports;
#: ranges and the evidence rule are validated by ``JudgeResult`` and Tier 2.
JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ENTAILED", "CONTRADICTED", "AMBIGUOUS"]},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["verdict", "reason", "confidence", "evidence"],
    "additionalProperties": False,
}

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_judge_output(raw: str) -> JudgeResult:
    """Strict parse; raises ``ValueError`` with a short reason on anything else."""
    text = _FENCE.sub("", raw.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not JSON: {exc.msg}") from exc
    try:
        return JudgeResult.model_validate(data)
    except ValidationError as exc:
        errs = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'root'}: {e['msg']}" for e in exc.errors()
        )
        raise ValueError(errs[:300]) from exc


class LLMJudge(ABC):
    provider = "llm"

    def __init__(
        self, *, model: str, prompt_version: str = "v1", prompt_dir: str | Path | None = None
    ) -> None:
        if not model:
            raise ValueError("a judge model identifier is required")
        self._model = model
        self._version = prompt_version
        self._judge_prompt = load_prompt("judge", prompt_version, prompt_dir)
        self._extract_prompt = load_prompt("extract", prompt_version, prompt_dir)

    @property
    def model_id(self) -> str:
        return f"{self.provider}:{self._model}"

    @property
    def prompt_version(self) -> str:
        return self._version

    @abstractmethod
    async def _complete(self, system: str, user: str, json_schema: dict[str, Any] | None) -> str:
        """One model call; returns its text. Raises ``DependencyUnavailable`` on failure."""

    async def judge(
        self,
        *,
        canonical_text: str,
        task_text: str,
        proposed_action: ProposedAction | None = None,
    ) -> JudgeResult:
        hint: str | None = None
        for _ in range(2):  # the answer, then one repair retry (§7.4)
            user = self._judge_prompt.render_user(
                canonical_text=canonical_text, task_text=task_text, repair_hint=hint
            )
            raw = await self._complete(self._judge_prompt.system, user, JUDGE_SCHEMA)
            try:
                return parse_judge_output(raw)
            except ValueError as exc:
                hint = str(exc)
        raise DependencyUnavailable("judge", f"invalid output after one retry: {hint}")

    async def extract_constraints(
        self,
        *,
        instruction: str,
        issued_at: datetime,
        repair_hint: str | None = None,
    ) -> str:
        user = self._extract_prompt.render_user(
            issued_at=issued_at.isoformat(), instruction=instruction, repair_hint=repair_hint
        )
        return await self._complete(self._extract_prompt.system, user, None)
