"""Judge contract (§6.3, §7.4). Real providers join in Phase 5 behind an opt-in flag."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from constraintlens.adapters.judge.scripted import ScriptedJudge
from constraintlens.domain.models import JudgeResult, ProposedAction
from constraintlens.domain.ports import Judge

if TYPE_CHECKING:
    from collections.abc import Callable

FACTORIES: list[Callable[[], Judge]] = [ScriptedJudge]


@pytest.fixture(params=FACTORIES, ids=lambda f: f.__name__)
def judge(request: pytest.FixtureRequest) -> Judge:
    return request.param()


def test_satisfies_protocol(judge: Judge) -> None:
    assert isinstance(judge, Judge)


def test_exposes_cache_key_identity(judge: Judge) -> None:
    assert judge.model_id
    assert judge.prompt_version


@pytest.mark.parametrize("action", [None, ProposedAction(tool="issue_refund", args={"amount": 5})])
async def test_returns_valid_result(judge: Judge, action: ProposedAction | None) -> None:
    result = await judge.judge(
        canonical_text="Refund up to 100 USD.",
        task_text="Refund the customer in full.",
        proposed_action=action,
    )
    assert isinstance(result, JudgeResult)
    assert result.verdict in {"ENTAILED", "CONTRADICTED", "AMBIGUOUS"}
    assert result.reason
    assert 0.0 <= result.confidence <= 1.0


async def test_extract_constraints_returns_text(judge: Judge) -> None:
    from datetime import UTC, datetime

    raw = await judge.extract_constraints(
        instruction="Refund up to $100.", issued_at=datetime(2026, 11, 7, tzinfo=UTC)
    )
    assert isinstance(raw, str)
