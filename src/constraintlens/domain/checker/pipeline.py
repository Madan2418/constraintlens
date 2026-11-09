"""TieredChecker: a registry of tiers run in order (ARCHITECTURE.md §7.4).

Each tier either returns a **terminal** outcome (later tiers are skipped) or passes
control on. Tiers are registered, not hard-coded (open/closed, §5), so Phase 5 adds
1b / 1c / 2 without editing this module.

The checker only *detects*. It returns findings; ``enforcement.decide`` maps them to
a decision, and the application layer records both (§5 "separation of detection,
decision, recording").

**Stale constraints (§9.3).** When the async guard marks a constraint stale at
CONSUME, a *semantic* tier (one with ``semantic = True``: Tier 1c or 2) must re-verify
it. After the tiers run, each stale constraint is either re-verified (a semantic tier
scored it: pass), failed (a semantic tier reported a finding on it: ``STALE``), or
unverifiable (no semantic tier covered it: the verdict is degraded for that
constraint and the fail policy decides, §10.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Protocol

from constraintlens.domain.async_guard import GuardAssessment, GuardConfig, assess
from constraintlens.domain.models import Failure
from constraintlens.domain.projections import HopHistory, default_idempotency_key

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from constraintlens.domain.models import CheckpointRequest, Constraint, TierId
    from constraintlens.domain.ports import Clock


@dataclass(frozen=True)
class Finding:
    """A detected failure plus what enforcement needs to know about it."""

    failure: Failure
    requires_approval: bool = False  # the action is allowed only with approval (§7.8)
    ambiguous: bool = False  # Tier 2 said AMBIGUOUS (§7.4 rule 5)
    held: bool = False  # a gap the checkpoint must wait out (§9.4, ADR 0006)


@dataclass
class Routing:
    """Per-check routing state the tiers share (§7.4 rules 3-4).

    ``escalated``: constraints a tier could not decide and passed on (the NLI band, an
    unavailable NLI model). ``reverified``: constraints a semantic tier has scored with
    a passing verdict, which settles their staleness (§9.3).
    """

    escalated: set[UUID] = field(default_factory=set)
    reverified: set[UUID] = field(default_factory=set)


@dataclass(frozen=True)
class CheckContext:
    request: CheckpointRequest
    registered: Sequence[Constraint]  # the service's authoritative set for the trace (§7.2)
    now: datetime
    history: HopHistory = field(default_factory=HopHistory)  # hops seen so far (§9.4)
    guard: GuardConfig = field(default_factory=GuardConfig)
    key: str = ""  # idempotency key of this checkpoint; default (trace, hop, checkpoint)
    routing: Routing = field(default_factory=Routing)

    def registered_by_id(self) -> dict[UUID, Constraint]:
        return {c.constraint_id: c for c in self.registered}

    @cached_property
    def assessment(self) -> GuardAssessment:
        """Async-guard findings for this checkpoint (computed once, pure)."""
        r = self.request
        key = self.key or default_idempotency_key(r.trace_id, r.hop_id, r.checkpoint)
        return assess(r, self.registered, self.history, self.now, self.guard, key=key)

    @property
    def stale(self) -> dict[UUID, str]:
        """Constraints a semantic tier must re-verify (§9.3); semantic tiers route on it."""
        return self.assessment.stale

    def needs_judgement(self) -> list[Constraint]:
        """Constraints the last semantic tier must decide: escalated, or stale and not
        yet re-verified by an earlier tier."""
        want = set(self.routing.escalated)
        want |= {cid for cid in self.stale if cid not in self.routing.reverified}
        return [c for c in self.registered if c.constraint_id in want]


@dataclass(frozen=True)
class TierOutcome:
    findings: list[Finding] = field(default_factory=list)
    terminal: bool = False
    scores: dict[UUID, float] = field(default_factory=dict)
    degraded: bool = False
    degraded_constraint_ids: list[UUID] = field(default_factory=list)
    cache_hit: bool = False
    escalate: list[UUID] = field(default_factory=list)  # undecided: pass to a later tier
    resolved: list[UUID] = field(default_factory=list)  # escalations this tier settled
    judge_failures: list[str] = field(default_factory=list)  # recorded as JUDGE_FAILED
    notes: list[str] = field(default_factory=list)


class Tier(Protocol):
    """A checker tier. Tiers that judge entailment set ``semantic = True`` (1c, 2)."""

    name: TierId

    def applies(self, ctx: CheckContext) -> bool: ...

    async def run(self, ctx: CheckContext) -> TierOutcome: ...


@dataclass(frozen=True)
class CheckResult:
    tier_used: TierId
    tiers_run: list[TierId]
    findings: list[Finding]
    scores: dict[UUID, float]
    latency_ms: dict[str, float]
    degraded: bool
    degraded_constraint_ids: list[UUID]
    cache_hit: bool
    findings_by_tier: dict[str, list[Finding]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # explanations enforcement adds to reasons
    judge_failures: list[str] = field(default_factory=list)

    @property
    def held(self) -> bool:
        return any(f.held for f in self.findings)


class TieredChecker:
    def __init__(self, tiers: Sequence[Tier], clock: Clock) -> None:
        if not tiers:
            raise ValueError("a checker needs at least one tier")
        names = [t.name for t in tiers]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate tier names: {names}")
        self._tiers = list(tiers)
        self._clock = clock

    @property
    def tier_names(self) -> list[TierId]:
        return [t.name for t in self._tiers]

    async def check(self, ctx: CheckContext) -> CheckResult:
        findings: list[Finding] = []
        scores: dict[UUID, float] = {}
        latency: dict[str, float] = {}
        run: list[TierId] = []
        degraded, cache_hit, integrity_stop, decided = False, False, False, False
        degraded_ids: list[UUID] = []
        by_tier: dict[str, list[Finding]] = {}
        notes: list[str] = []
        judge_failures: list[str] = []
        routing = ctx.routing
        for tier in self._tiers:
            if not tier.applies(ctx):
                continue
            start = self._clock.monotonic()
            outcome = await tier.run(ctx)
            latency[tier.name] = round((self._clock.monotonic() - start) * 1000, 3)
            run.append(tier.name)
            by_tier[tier.name] = list(outcome.findings)
            findings += outcome.findings
            scores.update(outcome.scores)
            degraded |= outcome.degraded
            degraded_ids += outcome.degraded_constraint_ids
            cache_hit |= outcome.cache_hit
            notes += outcome.notes
            judge_failures += outcome.judge_failures
            routing.escalated -= set(outcome.resolved)
            routing.escalated |= set(outcome.escalate)
            if getattr(tier, "semantic", False):
                unsettled = set(outcome.degraded_constraint_ids) | set(outcome.escalate)
                failed = {f.failure.constraint_id for f in outcome.findings}
                routing.reverified |= {
                    cid for cid in outcome.scores if cid not in unsettled and cid not in failed
                }
            if outcome.terminal:
                integrity_stop = tier.name == "T0" and bool(outcome.findings)
                decided = bool(outcome.findings)
                break
        if not run:  # pragma: no cover - Tier 0 always applies; defensive postcondition
            raise RuntimeError("no tier applied to the checkpoint")

        # Escalations no tier settled (no Tier 2, or it was skipped) are unverified: the
        # fail policy decides (§10.1). Moot once a finding already decided.
        if routing.escalated and not decided:
            degraded = True
            for cid in sorted(routing.escalated, key=str):
                degraded_ids.append(cid)
                notes.append(f"constraint {cid} undecided (escalated); no tier could settle it")

        if not integrity_stop:  # an integrity failure already decides; staleness is moot
            drift = {
                f.failure.constraint_id: f
                for f in findings
                if f.failure.kind == "TEXTUAL_DRIFT" and f.failure.constraint_id is not None
            }
            for cid, why in ctx.stale.items():
                if cid in drift:
                    stale = Finding(
                        Failure(
                            kind="STALE",
                            constraint_id=cid,
                            detail=f"{why}; re-verification failed: {drift[cid].failure.detail}",
                        )
                    )
                    findings.append(stale)
                    by_tier.setdefault(run[-1], []).append(stale)
                elif cid in routing.reverified:
                    notes.append(f"constraint {cid} stale ({why}); re-verified")
                elif not decided:
                    degraded = True
                    if cid not in degraded_ids:
                        degraded_ids.append(cid)
                    notes.append(
                        f"constraint {cid} stale ({why}); no semantic tier could re-verify it"
                    )
        return CheckResult(
            tier_used=run[-1],
            tiers_run=run,
            findings=findings,
            scores=scores,
            latency_ms=latency,
            degraded=degraded,
            degraded_constraint_ids=degraded_ids,
            cache_hit=cache_hit,
            findings_by_tier=by_tier,
            notes=notes,
            judge_failures=judge_failures,
        )
