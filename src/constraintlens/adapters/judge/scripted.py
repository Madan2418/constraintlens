"""``ScriptedJudge``: deterministic Judge fake for tests and CI smoke evaluation (§6.3, §19.3).

Judge responses are looked up by ``(canonical_text, task_text)``; unmatched calls get
the default. Extraction responses are looked up by instruction; a list of strings is
returned one per call (to script a bad first answer and its repair). Setting
``fail_with`` makes every call raise, which drives the degradation tests (§10.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from constraintlens.domain.models import JudgeResult

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from constraintlens.domain.models import ProposedAction


@dataclass(frozen=True)
class JudgeCall:
    canonical_text: str
    task_text: str
    proposed_action: ProposedAction | None


class ScriptedJudge:
    def __init__(
        self,
        responses: Mapping[tuple[str, str], JudgeResult] | None = None,
        *,
        default: JudgeResult | None = None,
        extractions: Mapping[str, str | Sequence[str]] | None = None,
        fail_with: Exception | None = None,
        model_id: str = "scripted",
        prompt_version: str = "v1",
    ) -> None:
        self._responses = dict(responses or {})
        self._default = default or JudgeResult(
            verdict="AMBIGUOUS", reason="no scripted response", confidence=0.5
        )
        self.fail_with = fail_with
        self._model_id = model_id
        self._prompt_version = prompt_version
        self.calls: list[JudgeCall] = []
        self._extractions = {
            k: [v] if isinstance(v, str) else list(v) for k, v in (extractions or {}).items()
        }
        self.extraction_calls: list[tuple[str, str | None]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    async def judge(
        self,
        *,
        canonical_text: str,
        task_text: str,
        proposed_action: ProposedAction | None = None,
    ) -> JudgeResult:
        self.calls.append(JudgeCall(canonical_text, task_text, proposed_action))
        if self.fail_with is not None:
            raise self.fail_with
        return self._responses.get((canonical_text, task_text), self._default)

    async def extract_constraints(
        self,
        *,
        instruction: str,
        issued_at: datetime,
        repair_hint: str | None = None,
    ) -> str:
        self.extraction_calls.append((instruction, repair_hint))
        if self.fail_with is not None:
            raise self.fail_with
        queue = self._extractions.get(instruction)
        if not queue:
            return '{"constraints": []}'
        return queue.pop(0) if len(queue) > 1 else queue[0]
