"""TieredChecker: a registry of tiers run in order (ARCHITECTURE.md §7.4).

Each tier either returns a **terminal** outcome (later tiers are skipped) or passes
control on. Tiers are registered, not hard-coded (open/closed, §5), so Phase 5 adds
1b / 1c / 2 without editing this module.

The checker only *detects*. It returns findings; ``enforcement.decide`` maps them to
a decision, and the application layer records both (§5 "separation of detection,
decision, recording").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from constraintlens.domain.models import CheckpointRequest, Constraint, Failure, TierId
    from constraintlens.domain.ports import Clock


@dataclass(frozen=True)
class Finding:
    """A detected failure plus what enforcement needs to know about it."""

    failure: Failure
    requires_approval: bool = False  # the action is allowed only with approval (§7.8)
    ambiguous: bool = False  # Tier 2 said AMBIGUOUS (§7.4 rule 5)


@dataclass(frozen=True)
class CheckContext:
    request: CheckpointRequest
    registered: Sequence[Constraint]  # the service's authoritative set for the trace (§7.2)
    now: datetime

    def registered_by_id(self) -> dict[UUID, Constraint]:
        return {c.constraint_id: c for c in self.registered}


@dataclass(frozen=True)
class TierOutcome:
    findings: list[Finding] = field(default_factory=list)
    terminal: bool = False
    scores: dict[UUID, float] = field(default_factory=dict)
    degraded: bool = False
    degraded_constraint_ids: list[UUID] = field(default_factory=list)
    cache_hit: bool = False


class Tier(Protocol):
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
        degraded, cache_hit = False, False
        degraded_ids: list[UUID] = []
        for tier in self._tiers:
            if not tier.applies(ctx):
                continue
            start = self._clock.monotonic()
            outcome = await tier.run(ctx)
            latency[tier.name] = round((self._clock.monotonic() - start) * 1000, 3)
            run.append(tier.name)
            findings += outcome.findings
            scores.update(outcome.scores)
            degraded |= outcome.degraded
            degraded_ids += outcome.degraded_constraint_ids
            cache_hit |= outcome.cache_hit
            if outcome.terminal:
                break
        if not run:  # pragma: no cover - Tier 0 always applies; defensive postcondition
            raise RuntimeError("no tier applied to the checkpoint")
        return CheckResult(
            tier_used=run[-1],
            tiers_run=run,
            findings=findings,
            scores=scores,
            latency_ms=latency,
            degraded=degraded,
            degraded_constraint_ids=degraded_ids,
            cache_hit=cache_hit,
        )
