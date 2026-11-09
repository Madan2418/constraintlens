"""Read-side use cases: close, summarise, replay, metrics (ARCHITECTURE.md §11.1, §12.1).

All of them are projections of the event log. ``ReplayTrace`` re-runs the checker and
enforcement over the stored checkpoint requests with different parameters and writes
nothing (§11.1 "no side effects"). It is how thresholds and ablations are explored
offline, and how "the event log is replayable" is proven (§23 Phase 3).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from constraintlens.domain.checker.pipeline import CheckContext
from constraintlens.domain.enforcement import Policy, decide
from constraintlens.domain.errors import UnknownTrace
from constraintlens.domain.models import CheckpointRequest, EventDraft
from constraintlens.domain.projections import TraceState, base_hop_id, fold

if TYPE_CHECKING:
    from uuid import UUID

    from constraintlens.domain.checker.pipeline import TieredChecker
    from constraintlens.domain.ports import Clock, EventStore


async def load_trace(store: EventStore, trace_id: UUID) -> TraceState:
    state = fold(trace_id, await store.read_trace(trace_id))
    if state.status == "unknown":
        raise UnknownTrace(f"trace {trace_id} was never opened")
    return state


class CloseTrace:
    def __init__(self, *, store: EventStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    async def __call__(self, trace_id: UUID) -> TraceState:
        state = await load_trace(self._store, trace_id)
        if state.status == "open":
            state.status = "closed"  # outcome below is computed for the closed trace
            await self._store.append(
                [
                    EventDraft(
                        trace_id=trace_id,
                        type="TRACE_CLOSED",
                        occurred_at=self._clock.now(),
                        payload={"outcome": state.outcome, "verdicts": len(state.verdicts)},
                    )
                ]
            )
            state = await load_trace(self._store, trace_id)
        return state


def summarise(state: TraceState) -> dict[str, Any]:
    return {
        "trace_id": str(state.trace_id),
        "status": state.status,
        "outcome": state.outcome,
        "constraints": [c.model_dump(mode="json") for c in state.constraints],
        "verdicts": [
            {
                "hop_id": v.hop_id,
                "checkpoint": v.checkpoint,
                "decision": v.decision,
                "tier_used": v.tier_used,
                "degraded": v.degraded,
                "failures": [f.model_dump(mode="json") for f in v.failures],
                "verdict_id": str(v.verdict_id),
            }
            for v in state.verdicts
        ],
        "last_event_id": state.last_event_id,
    }


@dataclass(frozen=True)
class ReplayItem:
    hop_id: str
    checkpoint: str
    original_decision: str
    replayed_decision: str
    replayed_reason: str

    @property
    def changed(self) -> bool:
        return self.original_decision != self.replayed_decision


class ReplayTrace:
    """Recompute every verdict of a trace from its events. Writes nothing."""

    def __init__(self, *, store: EventStore) -> None:
        self._store = store

    async def __call__(
        self, trace_id: UUID, *, checker: TieredChecker, policy: Policy
    ) -> list[ReplayItem]:
        events = await self._store.read_trace(trace_id)
        state = fold(trace_id, events)
        if state.status == "unknown":
            raise UnknownTrace(f"trace {trace_id} was never opened")
        regrounds: Counter[str] = Counter()
        items: list[ReplayItem] = []
        for event in events:
            if event.type != "CHECKPOINT_RECEIVED":
                continue
            key = event.payload["idempotency_key"]
            original = state.verdict_by_key.get(key)
            request = CheckpointRequest.model_validate(event.payload["request"])
            # Replay at the time the checkpoint was originally received: deterministic.
            result = await checker.check(
                CheckContext(request, state.constraints, event.occurred_at)
            )
            enf = decide(
                result,
                request.checkpoint,
                state.constraints,
                policy,
                regrounds_so_far=regrounds[base_hop_id(request.hop_id)],
            )
            if enf.decision == "RE_GROUND":
                regrounds[base_hop_id(request.hop_id)] += 1
            items.append(
                ReplayItem(
                    hop_id=request.hop_id,
                    checkpoint=request.checkpoint,
                    original_decision=original.decision if original else "UNRECORDED",
                    replayed_decision=enf.decision,
                    replayed_reason=enf.reason,
                )
            )
        return items


async def metrics(store: EventStore) -> dict[str, Any]:
    """Aggregates over all verdicts (§11.1 ``GET /v1/metrics``)."""
    decisions: Counter[str] = Counter()
    tiers: Counter[str] = Counter()
    degraded = cache_hits = total = redeliveries = 0
    latencies: dict[str, list[float]] = {}
    for e in await store.read_after(0):
        if e.type == "VERDICT_ISSUED":
            v = e.payload["verdict"]
            total += 1
            decisions[v["decision"]] += 1
            tiers[v["tier_used"]] += 1
            degraded += bool(v["degraded"])
            cache_hits += bool(v["cache_hit"])
            for tier, ms in v["latency_ms"].items():
                latencies.setdefault(tier, []).append(float(ms))
        elif e.type == "WARNING" and e.payload.get("redelivery"):
            redeliveries += 1
    return {
        "verdicts": total,
        "decisions": dict(decisions),
        "tier_used": dict(tiers),
        "degraded_rate": round(degraded / total, 4) if total else 0.0,
        "cache_hit_rate": round(cache_hits / total, 4) if total else 0.0,
        "redeliveries": redeliveries,
        "latency_ms": {t: _percentiles(v) for t, v in sorted(latencies.items())},
    }


def _percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def pct(p: float) -> float:
        idx = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
        return ordered[idx]

    return {"p50": pct(50), "p95": pct(95), "p99": pct(99), "n": float(len(ordered))}
