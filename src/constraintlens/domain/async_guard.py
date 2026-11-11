"""Async guard: staleness, ordering and gaps at the queue boundary (ARCHITECTURE.md §7.5, §9).

Pure functions over the request, the registered constraints, and the trace's hop
history (rebuilt from the event log, ``projections.HopHistory``). Tier 0 calls
``assess`` at every checkpoint; only ``CONSUME`` produces async findings.

* **Staleness (§9.3)**: a constraint is stale when its age exceeds ``max_age_seconds``
  or the message's queue dwell exceeds ``dwell_threshold_s``. Stale is not violated: it
  demands re-verification by a semantic tier (1c/2). The checker enforces that demand;
  if no semantic tier can re-verify, the verdict is degraded and the fail policy
  decides (§10.1).
* **Gaps (§9.4)**: the consumed envelope names a predecessor never observed, or its
  ``hop_seq`` skips past its parents'. With any high-risk constraint the checkpoint is
  *held* (a non-final BLOCK) until the predecessor appears or the join timeout passes.
  After the timeout, or for low risk, it proceeds with a ``MISSING_HOP`` warning.
  Waiting time is measured from the first hold on the service clock, never from a
  client-supplied timestamp, so a producer cannot backdate its way past the hold.
* **Inversions (§9.4)**: an envelope whose descendants were already consumed is a late
  delivery. It is checked and flagged ``OUT_OF_ORDER``; it never moves the trace's
  frontier backwards, because the history only ever grows.

Redelivery (§9.6) is handled by idempotent verdicts in ``VerifyCheckpoint``. Loss in
transit case (d), a publish never consumed, is the sweeper's job (``sweep_transit``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from constraintlens.domain.models import Failure

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from constraintlens.domain.models import CheckpointRequest, Constraint
    from constraintlens.domain.projections import HopHistory


@dataclass(frozen=True)
class GuardConfig:
    dwell_threshold_s: float = 300.0
    join_timeout_s: float = 60.0
    #: False only for the "no async guard" ablation (§14.2 C3, ADR 0010): no staleness,
    #: ordering or gap findings at all.
    enabled: bool = True


@dataclass(frozen=True)
class Readiness:
    missing_parents: list[str]
    seq_gap: int  # hops skipped between the observed parents and this envelope
    waited_s: float
    timed_out: bool

    @property
    def ready(self) -> bool:
        return not self.missing_parents and self.seq_gap == 0


@dataclass(frozen=True)
class GuardAssessment:
    failures: list[Failure] = field(default_factory=list)
    held: bool = False  # the gap failure blocks until resolved (high risk, before timeout)
    stale: dict[UUID, str] = field(default_factory=dict)  # constraint -> why it needs re-check


def dwell_seconds(request: CheckpointRequest, now: datetime) -> float | None:
    meta = request.transport_meta
    if meta is None or meta.enqueued_at is None:
        return None
    consumed = meta.consumed_at or now
    return max(0.0, (consumed - meta.enqueued_at).total_seconds())  # clamp clock skew


def stale_constraints(
    request: CheckpointRequest,
    registered: Sequence[Constraint],
    now: datetime,
    config: GuardConfig,
) -> dict[UUID, str]:
    """``constraint_id -> why`` for every constraint needing re-verification at CONSUME."""
    if request.checkpoint != "CONSUME" or not config.enabled:
        return {}
    dwell = dwell_seconds(request, now)
    out: dict[UUID, str] = {}
    for c in registered:
        age = (now - c.issued_at).total_seconds()
        if c.max_age_seconds is not None and age > c.max_age_seconds:
            out[c.constraint_id] = f"age {age:.0f}s exceeds max_age {c.max_age_seconds}s"
        elif dwell is not None and dwell > config.dwell_threshold_s:
            out[c.constraint_id] = (
                f"queue dwell {dwell:.0f}s exceeds {config.dwell_threshold_s:.0f}s"
            )
    return out


def readiness(
    request: CheckpointRequest,
    history: HopHistory,
    now: datetime,
    config: GuardConfig,
    *,
    key: str,
) -> Readiness:
    """Are all predecessors of the consumed envelope known? (gap / fan-in join, §9.4)"""
    if request.checkpoint != "CONSUME":
        return Readiness([], 0, 0.0, False)
    hop = request.envelope.hop
    missing = [p for p in hop.parent_hop_ids if not history.observed(p)]
    parent_seqs = [history.seq[p] for p in hop.parent_hop_ids if p in history.seq]
    gap = 0
    if parent_seqs and not missing:
        gap = max(0, hop.hop_seq - max(parent_seqs) - 1)
    since = history.held_since.get(key)
    waited = max(0.0, (now - since).total_seconds()) if since else 0.0
    return Readiness(missing, gap, waited, timed_out=waited >= config.join_timeout_s)


def is_late_delivery(request: CheckpointRequest, history: HopHistory) -> bool:
    """A descendant of this envelope's hop was already consumed: it arrives out of order."""
    if request.checkpoint != "CONSUME":
        return False
    return history.has_consumed_descendant(request.envelope.hop.hop_id)


def seq_not_after_parents(request: CheckpointRequest, history: HopHistory) -> bool:
    """``hop_seq`` must exceed every observed parent's (monotonic along a path, §9.2)."""
    hop = request.envelope.hop
    seqs = [history.seq[p] for p in hop.parent_hop_ids if p in history.seq]
    return bool(seqs) and hop.hop_seq <= max(seqs)


def assess(
    request: CheckpointRequest,
    registered: Sequence[Constraint],
    history: HopHistory,
    now: datetime,
    config: GuardConfig,
    *,
    key: str,
) -> GuardAssessment:
    """Every async finding for one checkpoint. Empty unless it is a ``CONSUME``."""
    if request.checkpoint != "CONSUME" or not config.enabled:
        return GuardAssessment()
    failures: list[Failure] = []
    hop = request.envelope.hop

    if is_late_delivery(request, history):
        failures.append(
            Failure(
                kind="OUT_OF_ORDER",
                constraint_id=None,
                detail=f"late delivery: hops downstream of {hop.hop_id} were already consumed",
            )
        )
    elif seq_not_after_parents(request, history):
        failures.append(
            Failure(
                kind="OUT_OF_ORDER",
                constraint_id=None,
                detail=f"hop_seq {hop.hop_seq} of {hop.hop_id} is not after its parents'",
            )
        )

    ready = readiness(request, history, now, config, key=key)
    held = False
    if not ready.ready:
        high = not registered or any(c.risk_class == "high" for c in registered)
        held = high and not ready.timed_out
        what = (
            f"predecessor(s) {', '.join(ready.missing_parents)} never observed"
            if ready.missing_parents
            else f"{ready.seq_gap} hop(s) missing before {hop.hop_id} (hop_seq gap)"
        )
        if held:
            detail = f"{what}; held until they arrive or {config.join_timeout_s:.0f}s pass"
        elif high:
            detail = f"{what}; join timeout of {config.join_timeout_s:.0f}s elapsed, proceeding"
        else:
            detail = f"{what}; low-risk constraints only, proceeding"
        failures.append(Failure(kind="MISSING_HOP", constraint_id=None, detail=detail))

    return GuardAssessment(
        failures=failures,
        held=held,
        stale=stale_constraints(request, registered, now, config),
    )
