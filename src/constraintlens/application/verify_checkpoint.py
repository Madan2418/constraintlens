"""``VerifyCheckpoint`` use case (ARCHITECTURE.md §6.4, §7.4, §7.8, §7.9, §9.6, §11.1).

check (detect) → decide (enforce) → record → respond, in that order:

* **Idempotent** on ``(trace_id, hop_id, checkpoint)`` or a client ``Idempotency-Key``.
  A duplicate gets the stored verdict unchanged, plus a ``WARNING`` event with
  ``redelivery=true``. A duplicate that arrives while the first is still in flight
  waits on a per-key lock instead of computing a second, possibly conflicting verdict.
* **Recorded before responding**: CHECKPOINT_RECEIVED, TIER_EXECUTED (per tier),
  VERDICT_ISSUED, DECISION_ENFORCED and, when re-grounding, REGROUND_ISSUED are
  appended in one atomic batch.
* **Recorder failure** fails closed for high-risk constraints and open with a warning
  for low-risk ones (§7.9, §10.1). Either way the verdict is marked ``degraded``.
* Task text is redacted before persistence unless ``store_full_text`` (§12.4).
* **Conflicting duplicates** (ADR 0006): a request with a stored verdict's key whose
  constraints, digest, hop or proposed action differ is not a redelivery (tampering,
  or a changed tool call). It is checked afresh and recorded as a non-final
  ``WARNING`` with ``conflict``; the stored verdict stays the key's answer.
* **Held checkpoints** (§9.4, ADR 0006): a CONSUME whose predecessor is missing gets a
  *held* BLOCK. It is recorded (CHECKPOINT_RECEIVED with ``held``, TIER_EXECUTED, a
  ``WARNING`` carrying the verdict, DECISION_ENFORCED with ``final=false``) but is not
  stored as the idempotent answer. The redelivered message is checked again, with the
  history the log now holds, until the gap closes or the join times out.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from constraintlens.domain.async_guard import GuardConfig
from constraintlens.domain.checker.pipeline import CheckContext
from constraintlens.domain.enforcement import DECISION_RANK, Policy, decide
from constraintlens.domain.errors import DependencyUnavailable, InvalidEnvelope, UnknownTrace
from constraintlens.domain.models import EventDraft, Verdict
from constraintlens.domain.projections import (
    base_hop_id,
    default_idempotency_key,
    fold,
    request_fingerprint,
)
from constraintlens.domain.redaction import ORDER, redact

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from constraintlens.domain.checker.pipeline import CheckResult, TieredChecker
    from constraintlens.domain.enforcement import Enforcement
    from constraintlens.domain.models import CheckpointRequest, Constraint
    from constraintlens.domain.ports import Clock, EventStore


def request_record(
    request: CheckpointRequest, *, store_full_text: bool, redact_patterns: Iterable[str]
) -> dict[str, Any]:
    """The checkpoint request as persisted: JSON, task text redacted unless opted in."""
    data = request.model_dump(mode="json")
    if not store_full_text:
        data["envelope"]["payload_text"] = redact(data["envelope"]["payload_text"], redact_patterns)
    return data


def build_verdict(
    *,
    verdict_id: UUID,
    request: CheckpointRequest,
    result: CheckResult,
    enforcement: Enforcement,
) -> Verdict:
    return Verdict(
        verdict_id=verdict_id,
        trace_id=request.trace_id,
        hop_id=request.hop_id,
        checkpoint=request.checkpoint,
        tier_used=result.tier_used,
        scores=result.scores,
        failures=[f.failure for f in result.findings],
        decision=enforcement.decision,
        degraded=result.degraded,
        reason=enforcement.reason,
        latency_ms=result.latency_ms,
        cache_hit=result.cache_hit,
        regrounded_text=enforcement.regrounded_text,
        held=result.held,
    )


@dataclass(frozen=True)
class CheckpointOutcome:
    verdict: Verdict
    replayed: bool  # the stored verdict of an earlier identical request (§9.6)


class VerifyCheckpoint:
    def __init__(
        self,
        *,
        store: EventStore,
        clock: Clock,
        checker: TieredChecker,
        policy: Policy,
        store_full_text: bool = False,
        redact_patterns: Iterable[str] = ORDER,
        id_factory: Callable[[], UUID] = uuid4,
        guard: GuardConfig | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._checker = checker
        self._policy = policy
        self._store_full_text = store_full_text
        self._redact = tuple(redact_patterns)
        self._new_id = id_factory
        self._guard = guard or GuardConfig()
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def checker(self) -> TieredChecker:
        return self._checker

    async def __call__(
        self, request: CheckpointRequest, *, idempotency_key: str | None = None
    ) -> Verdict:
        return (await self.run(request, idempotency_key=idempotency_key)).verdict

    async def run(
        self, request: CheckpointRequest, *, idempotency_key: str | None = None
    ) -> CheckpointOutcome:
        key = idempotency_key or default_idempotency_key(
            request.trace_id, request.hop_id, request.checkpoint
        )
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await self._verify(request, key)

    async def _verify(self, request: CheckpointRequest, key: str) -> CheckpointOutcome:
        try:
            events = await self._store.read_trace(request.trace_id)
        except DependencyUnavailable:
            raise
        except Exception as exc:  # store adapters should raise DependencyUnavailable
            raise DependencyUnavailable("event store", str(exc)) from exc
        state = fold(request.trace_id, events)
        if state.status == "unknown":
            raise UnknownTrace(f"trace {request.trace_id} was never opened")

        triple = default_idempotency_key(request.trace_id, request.hop_id, request.checkpoint)
        fingerprint = request_fingerprint(request)
        stored = state.stored_verdict(key, triple)
        stored_fp = state.fingerprint_by_checkpoint.get(triple)
        conflict = stored is not None and stored_fp is not None and stored_fp != fingerprint
        if stored is not None and not conflict:
            await self._store.append(
                [
                    EventDraft(
                        trace_id=request.trace_id,
                        type="WARNING",
                        hop_id=request.hop_id,
                        occurred_at=self._clock.now(),
                        payload={
                            "redelivery": True,
                            "idempotency_key": key,
                            "verdict_id": str(stored.verdict_id),
                            "delivery_count": (
                                request.transport_meta.delivery_count
                                if request.transport_meta
                                else None
                            ),
                        },
                    )
                ]
            )
            return CheckpointOutcome(stored, replayed=True)
        if state.status != "open":
            raise InvalidEnvelope(f"trace {request.trace_id} is {state.status}")

        now = self._clock.now()
        result = await self._checker.check(
            CheckContext(request, state.constraints, now, history=state.history, guard=self._guard)
        )
        enforcement = decide(
            result,
            request.checkpoint,
            state.constraints,
            self._policy,
            regrounds_so_far=state.regrounds_by_hop.get(base_hop_id(request.hop_id), 0),
        )
        verdict = build_verdict(
            verdict_id=self._new_id(), request=request, result=result, enforcement=enforcement
        )
        drafts = self._events(
            request, key, result, verdict, now,
            fingerprint=fingerprint,
            conflict_with=stored.verdict_id if conflict and stored else None,
        )  # fmt: skip
        try:
            await self._store.append(drafts)
        except Exception as exc:
            return CheckpointOutcome(
                self._recorder_failed(verdict, state.constraints, exc), replayed=False
            )
        return CheckpointOutcome(verdict, replayed=False)

    def _events(
        self,
        request: CheckpointRequest,
        key: str,
        result: CheckResult,
        verdict: Verdict,
        now: Any,
        *,
        fingerprint: str,
        conflict_with: UUID | None = None,
    ) -> list[EventDraft]:
        def ev(type_: str, payload: dict[str, Any]) -> EventDraft:
            return EventDraft(
                trace_id=request.trace_id,
                type=type_,  # type: ignore[arg-type]
                hop_id=request.hop_id,
                occurred_at=now,
                payload=payload,
            )

        received: dict[str, Any] = {
            "idempotency_key": key,
            "request": request_record(
                request, store_full_text=self._store_full_text, redact_patterns=self._redact
            ),
        }
        received["fingerprint"] = fingerprint
        if verdict.held:
            received["held"] = True
        if conflict_with is not None:
            received["conflict"] = True
        drafts = [ev("CHECKPOINT_RECEIVED", received)]
        for tier in result.tiers_run:
            drafts.append(
                ev(
                    "TIER_EXECUTED",
                    {
                        "tier": tier,
                        "latency_ms": result.latency_ms.get(tier),
                        "failures": [
                            f.failure.model_dump(mode="json")
                            for f in result.findings_by_tier.get(tier, [])
                        ],
                    },
                )
            )
        for failure in result.judge_failures:  # §8.4, §10.1: the degraded path is visible
            drafts.append(ev("JUDGE_FAILED", {"tier": "T2", "detail": failure}))
        verdict_json = verdict.model_dump(mode="json")
        if conflict_with is not None:
            # Same key, different request: not a redelivery (ADR 0006). Checked afresh,
            # recorded, but the stored verdict stays the key's answer.
            drafts.append(
                ev(
                    "WARNING",
                    {
                        "conflict": True,
                        "held": verdict.held,
                        "idempotency_key": key,
                        "conflicts_with_verdict_id": str(conflict_with),
                        "verdict": verdict_json,
                    },
                )
            )
        elif verdict.held:
            # Recorded, but not the idempotent answer: the checkpoint will be re-checked.
            drafts.append(
                ev("WARNING", {"held": True, "idempotency_key": key, "verdict": verdict_json})
            )
        else:
            drafts.append(ev("VERDICT_ISSUED", {"idempotency_key": key, "verdict": verdict_json}))
        drafts.append(
            ev(
                "DECISION_ENFORCED",
                {
                    "verdict_id": str(verdict.verdict_id),
                    "checkpoint": request.checkpoint,
                    "decision": verdict.decision,
                    "final": not verdict.held and conflict_with is None,
                },
            )
        )
        if verdict.decision == "RE_GROUND":
            drafts.append(
                ev(
                    "REGROUND_ISSUED",
                    {"verdict_id": str(verdict.verdict_id), "text": verdict.regrounded_text},
                )
            )
        return drafts

    def _recorder_failed(
        self, verdict: Verdict, constraints: list[Constraint], exc: Exception
    ) -> Verdict:
        """§7.9: a decision never exists without a record, so an unrecordable one degrades."""
        high = not constraints or any(c.risk_class == "high" for c in constraints)
        floor = (
            "BLOCK" if high and self._policy.fail_high_risk == "closed" else "ALLOW_WITH_WARNING"
        )
        decision = (
            verdict.decision if DECISION_RANK[verdict.decision] >= DECISION_RANK[floor] else floor
        )
        return verdict.model_copy(
            update={
                "decision": decision,
                "degraded": True,
                "reason": f"{verdict.reason}; event store unavailable ({exc.__class__.__name__}),"
                f" verdict not recorded, failing {'closed' if decision == 'BLOCK' else 'open'}",
                "regrounded_text": verdict.regrounded_text if decision == "RE_GROUND" else None,
            }
        )
