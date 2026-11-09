"""Every row of the §7.8 decision table has a test (§20), plus fail policy and re-grounding."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from constraintlens.domain.canonical import issue
from constraintlens.domain.checker.pipeline import CheckResult, Finding
from constraintlens.domain.enforcement import (
    DECISION_TABLE,
    Policy,
    decide,
)
from constraintlens.domain.models import ConstraintSpec, Failure, Limit
from tests.factories import T0

HIGH = issue(
    ConstraintSpec(owner="u", action="refund", resource="order",
                   limit=Limit(max_value=Decimal(100), unit="USD")),
    constraint_id=UUID(int=1), issued_at=T0,
)  # fmt: skip
LOW = issue(
    ConstraintSpec(owner="u", action="refund", resource="order", risk_class="low",
                   limit=Limit(max_value=Decimal(500), unit="USD")),
    constraint_id=UUID(int=2), issued_at=T0,
)  # fmt: skip


def result(*findings: Finding, degraded: bool = False, degraded_ids: Any = ()) -> CheckResult:
    return CheckResult(
        tier_used="T1a",
        tiers_run=["T0", "T1a"],
        findings=list(findings),
        scores={},
        latency_ms={"T0": 0.1},
        degraded=degraded,
        degraded_constraint_ids=list(degraded_ids),
        cache_hit=False,
    )


def finding(kind: str, cid: UUID | None, **kw: Any) -> Finding:
    return Finding(Failure(kind=kind, constraint_id=cid, detail="d"), **kw)  # type: ignore[arg-type]


def decision(kind: str, checkpoint: str, risk: str, policy: Policy | None = None, **kw: Any) -> str:
    c = HIGH if risk == "high" else LOW
    return decide(
        result(finding(kind, c.constraint_id, **kw)),
        checkpoint,
        [HIGH, LOW],
        policy or Policy(),  # type: ignore[arg-type]
    ).decision


# (failure kind, checkpoint, high-risk decision, low-risk decision) — ARCHITECTURE.md §7.8
SPEC_ROWS = [
    ("ACTION_VIOLATION", "TOOL_CALL", "BLOCK", "BLOCK"),
    ("TEXTUAL_DRIFT", "TOOL_CALL", "BLOCK", "RE_GROUND"),
    ("TEXTUAL_DRIFT", "PUBLISH", "RE_GROUND", "ALLOW_WITH_WARNING"),
    ("TEXTUAL_DRIFT", "CONSUME", "RE_GROUND", "ALLOW_WITH_WARNING"),
    ("CONSTRAINT_LOST", "CONSUME", "BLOCK", "BLOCK"),
    ("CONSTRAINT_DROPPED", "PUBLISH", "BLOCK", "BLOCK"),
    ("HASH_MISMATCH", "TOOL_CALL", "BLOCK", "BLOCK"),
    ("STALE", "CONSUME", "BLOCK", "RE_GROUND"),  # stale, re-verification fails
]


@pytest.mark.parametrize(("kind", "checkpoint", "high", "low"), SPEC_ROWS)
def test_decision_table_rows(kind: str, checkpoint: str, high: str, low: str) -> None:
    assert decision(kind, checkpoint, "high") == high
    assert decision(kind, checkpoint, "low") == low


def test_stale_with_passing_reverification_allows() -> None:
    # "Stale beyond bound, re-verification passes → ALLOW": no finding is produced.
    assert decide(result(), "CONSUME", [HIGH], Policy()).decision == "ALLOW"


def test_ambiguous_after_tier2() -> None:
    assert decision("TEXTUAL_DRIFT", "TOOL_CALL", "high", ambiguous=True) == "BLOCK"
    assert decision("TEXTUAL_DRIFT", "TOOL_CALL", "low", ambiguous=True) == "ALLOW_WITH_WARNING"


@pytest.mark.parametrize(
    ("high_mode", "low_mode", "risk", "expected"),
    [
        ("closed", "open", "high", "BLOCK"),
        ("closed", "open", "low", "ALLOW_WITH_WARNING"),
        ("open", "open", "high", "ALLOW_WITH_WARNING"),
        ("closed", "closed", "low", "BLOCK"),
    ],
)
def test_judge_unavailable_follows_fail_policy(
    high_mode: str, low_mode: str, risk: str, expected: str
) -> None:
    c = HIGH if risk == "high" else LOW
    policy = Policy(fail_high_risk=high_mode, fail_low_risk=low_mode)  # type: ignore[arg-type]
    enf = decide(
        result(degraded=True, degraded_ids=[c.constraint_id]), "TOOL_CALL", [HIGH, LOW], policy
    )
    assert enf.decision == expected
    assert "degraded" in enf.reason


def test_degraded_without_named_constraints_is_treated_as_high_risk() -> None:
    assert decide(result(degraded=True), "PUBLISH", [LOW], Policy()).decision == "BLOCK"


def test_extra_rows_not_in_spec_table() -> None:
    assert decision("EXPIRED", "TOOL_CALL", "low") == "BLOCK"
    assert decision("UNSATISFIABLE", "PUBLISH", "low") == "BLOCK"
    # ADR 0006: a gap holds (BLOCK, not final) until resolved or timed out; what then
    # proceeds is flagged.
    assert decision("MISSING_HOP", "CONSUME", "high", held=True) == "BLOCK"
    assert decision("MISSING_HOP", "CONSUME", "high") == "ALLOW_WITH_WARNING"
    assert decision("MISSING_HOP", "CONSUME", "low") == "ALLOW_WITH_WARNING"
    assert decision("OUT_OF_ORDER", "CONSUME", "high") == "ALLOW_WITH_WARNING"
    assert {k for k, _ in DECISION_TABLE} >= {
        "ACTION_VIOLATION", "TEXTUAL_DRIFT", "CONSTRAINT_LOST", "CONSTRAINT_DROPPED",
        "HASH_MISMATCH", "STALE", "EXPIRED", "UNSATISFIABLE", "MISSING_HOP", "OUT_OF_ORDER",
    }  # fmt: skip


def test_approval_breach_escalates_only_with_a_route() -> None:
    assert decision("ACTION_VIOLATION", "TOOL_CALL", "high", requires_approval=True) == "BLOCK"
    route = Policy(escalate_approvals=True)
    assert decision("ACTION_VIOLATION", "TOOL_CALL", "high", route, requires_approval=True) == (
        "ESCALATE"
    )
    assert decision("ACTION_VIOLATION", "TOOL_CALL", "high", route) == "BLOCK"


def test_strictest_wins_and_block_outranks_escalate() -> None:
    enf = decide(
        result(
            finding("ACTION_VIOLATION", HIGH.constraint_id, requires_approval=True),
            finding("ACTION_VIOLATION", LOW.constraint_id),
            finding("TEXTUAL_DRIFT", LOW.constraint_id),
        ),
        "TOOL_CALL",
        [HIGH, LOW],
        Policy(escalate_approvals=True),
    )
    assert enf.decision == "BLOCK"
    assert enf.reason.count("ACTION_VIOLATION") == 2


def test_unknown_constraint_is_high_risk_and_cannot_be_regrounded() -> None:
    # High risk → RE_GROUND at PUBLISH, but there is no registered text to re-inject,
    # so the decision falls back to BLOCK (fail-safe).
    enf = decide(result(finding("TEXTUAL_DRIFT", uuid4())), "PUBLISH", [LOW], Policy())
    assert enf.decision == "BLOCK"
    assert enf.regrounded_text is None


def test_reground_text_and_limit() -> None:
    drift = result(finding("TEXTUAL_DRIFT", HIGH.constraint_id))
    first = decide(drift, "PUBLISH", [HIGH], Policy(max_regrounds=2), regrounds_so_far=1)
    assert first.decision == "RE_GROUND"
    assert first.regrounded_text == HIGH.canonical_text
    exhausted = decide(drift, "PUBLISH", [HIGH], Policy(max_regrounds=2), regrounds_so_far=2)
    assert exhausted.decision == "BLOCK"
    assert "re-ground limit" in exhausted.reason


def test_clean_result_allows_with_reason() -> None:
    enf = decide(result(), "PUBLISH", [HIGH], Policy())
    assert (enf.decision, enf.regrounded_text) == ("ALLOW", None)
    assert enf.reason == "all checks passed: T0, T1a"
