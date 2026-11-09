"""Enforcement: map findings to a decision (ARCHITECTURE.md §7.8, §10.2, ADR 0005).

The §7.8 decision table is data (``DECISION_TABLE``), so every row is visible and
testable. Each finding is decided by its failure kind, the checkpoint, and the risk
class of the constraint it names; the strictest decision wins.

Strictness: ALLOW < ALLOW_WITH_WARNING < RE_GROUND < ESCALATE < BLOCK. A hard BLOCK
outranks ESCALATE, because approval cannot cure a hard-cap breach.

Approval-threshold breaches become ESCALATE only when an approval route is configured
(``Policy.escalate_approvals``). Otherwise they BLOCK, as in the worked example (§2.1).

A *held* finding (a gap at CONSUME, §9.4) always BLOCKs; the verdict is marked held and
is not final (ADR 0006).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from constraintlens.domain.models import Checkpoint, Decision, FailureKind, RiskClass

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID

    from constraintlens.domain.checker.pipeline import CheckResult, Finding
    from constraintlens.domain.models import Constraint

FailMode = Literal["open", "closed"]

DECISION_RANK: dict[str, int] = {
    "ALLOW": 0,
    "ALLOW_WITH_WARNING": 1,
    "RE_GROUND": 2,
    "ESCALATE": 3,
    "BLOCK": 4,
}

Row = tuple[Decision, Decision]  # (high-risk, low-risk)
_AT_GATE: tuple[Checkpoint, ...] = ("TOOL_CALL",)

#: §7.8 decision table, keyed by (failure kind, at TOOL_CALL?).
DECISION_TABLE: dict[tuple[FailureKind, bool], Row] = {
    ("ACTION_VIOLATION", True): ("BLOCK", "BLOCK"),
    ("ACTION_VIOLATION", False): ("BLOCK", "BLOCK"),
    ("TEXTUAL_DRIFT", True): ("BLOCK", "RE_GROUND"),
    ("TEXTUAL_DRIFT", False): ("RE_GROUND", "ALLOW_WITH_WARNING"),
    ("CONSTRAINT_LOST", True): ("BLOCK", "BLOCK"),
    ("CONSTRAINT_LOST", False): ("BLOCK", "BLOCK"),
    ("CONSTRAINT_DROPPED", True): ("BLOCK", "BLOCK"),
    ("CONSTRAINT_DROPPED", False): ("BLOCK", "BLOCK"),
    ("HASH_MISMATCH", True): ("BLOCK", "BLOCK"),
    ("HASH_MISMATCH", False): ("BLOCK", "BLOCK"),
    # "Stale beyond bound, re-verification fails" (a passing re-check yields no finding).
    ("STALE", True): ("BLOCK", "RE_GROUND"),
    ("STALE", False): ("BLOCK", "RE_GROUND"),
    # Not in the §7.8 table; decided here (ADR 0005).
    ("EXPIRED", True): ("BLOCK", "BLOCK"),
    ("EXPIRED", False): ("BLOCK", "BLOCK"),
    ("UNSATISFIABLE", True): ("BLOCK", "BLOCK"),
    ("UNSATISFIABLE", False): ("BLOCK", "BLOCK"),
    # §9.4: a gap first *holds* a high-risk checkpoint (Finding.held -> BLOCK, not final).
    # The row covers what proceeds: a join that timed out, or low-risk constraints only.
    ("MISSING_HOP", True): ("ALLOW_WITH_WARNING", "ALLOW_WITH_WARNING"),  # ADR 0006
    ("MISSING_HOP", False): ("ALLOW_WITH_WARNING", "ALLOW_WITH_WARNING"),
    ("OUT_OF_ORDER", True): ("ALLOW_WITH_WARNING", "ALLOW_WITH_WARNING"),  # §9.4: checked, flagged
    ("OUT_OF_ORDER", False): ("ALLOW_WITH_WARNING", "ALLOW_WITH_WARNING"),
}
#: "AMBIGUOUS after Tier 2" row (§7.8).
AMBIGUOUS_ROW: Row = ("BLOCK", "ALLOW_WITH_WARNING")


@dataclass(frozen=True)
class Policy:
    fail_high_risk: FailMode = "closed"
    fail_low_risk: FailMode = "open"
    max_regrounds: int = 2
    escalate_approvals: bool = False  # True once a human-approval route is configured


@dataclass(frozen=True)
class Enforcement:
    decision: Decision
    reason: str
    regrounded_text: str | None


def _pick(row: Row, risk: RiskClass) -> Decision:
    return row[0] if risk == "high" else row[1]


def _risk_of(constraint_id: UUID | None, registered: Mapping[UUID, Constraint]) -> RiskClass:
    if constraint_id is None or constraint_id not in registered:
        return "high"  # fail-safe default (§5)
    return registered[constraint_id].risk_class


def decide_finding(
    finding: Finding,
    checkpoint: Checkpoint,
    registered: Mapping[UUID, Constraint],
    policy: Policy,
) -> Decision:
    f = finding.failure
    risk = _risk_of(f.constraint_id, registered)
    if finding.held:
        return "BLOCK"  # wait for the missing predecessor (§9.4)
    if finding.ambiguous:
        return _pick(AMBIGUOUS_ROW, risk)
    decision = _pick(DECISION_TABLE[(f.kind, checkpoint in _AT_GATE)], risk)
    if (
        f.kind == "ACTION_VIOLATION"
        and finding.requires_approval
        and decision == "BLOCK"
        and policy.escalate_approvals
    ):
        return "ESCALATE"
    return decision


def decide(
    result: CheckResult,
    checkpoint: Checkpoint,
    registered: Sequence[Constraint],
    policy: Policy,
    *,
    regrounds_so_far: int = 0,
) -> Enforcement:
    """Strictest decision over all findings, plus the fail policy for degraded tiers."""
    by_id = {c.constraint_id: c for c in registered}
    decision: Decision = "ALLOW"
    reasons: list[str] = []
    reground_ids: list[UUID] = []

    for finding in result.findings:
        d = decide_finding(finding, checkpoint, by_id, policy)
        if d == "RE_GROUND" and finding.failure.constraint_id is not None:
            reground_ids.append(finding.failure.constraint_id)
        if DECISION_RANK[d] > DECISION_RANK[decision]:
            decision = d
        reasons.append(f"{finding.failure.kind}: {finding.failure.detail}")

    reasons += result.notes
    if result.degraded:
        for cid in result.degraded_constraint_ids or [None]:  # type: ignore[list-item]
            mode = policy.fail_high_risk if _risk_of(cid, by_id) == "high" else policy.fail_low_risk
            d = "BLOCK" if mode == "closed" else "ALLOW_WITH_WARNING"
            if DECISION_RANK[d] > DECISION_RANK[decision]:
                decision = d
        reasons.append("verification degraded: a tier was unavailable (fail policy applied)")

    regrounded_text: str | None = None
    if decision == "RE_GROUND":
        if regrounds_so_far >= policy.max_regrounds:
            decision = "BLOCK"
            reasons.append(f"re-ground limit of {policy.max_regrounds} reached")
        else:
            texts = [by_id[i].canonical_text for i in dict.fromkeys(reground_ids) if i in by_id]
            regrounded_text = " ".join(texts) or None
            if regrounded_text is None:
                # Nothing registered to re-inject (unknown constraint): fail safe.
                decision = "BLOCK"
                reasons.append("no registered constraint text to re-ground with")

    if not reasons:
        reasons.append("all checks passed: " + ", ".join(result.tiers_run))
    return Enforcement(
        decision=decision, reason="; ".join(reasons), regrounded_text=regrounded_text
    )
