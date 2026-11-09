"""Trace state as a pure projection of the event log (ARCHITECTURE.md §5, §12.1).

The event log is the single source of truth. Everything the use cases need about a
trace (its registered constraints, status, stored verdicts, re-ground counts) is
derived here by folding its events. The same fold drives replay and the trace
summary, so they cannot disagree with what actually happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from constraintlens.domain.models import Constraint, Verdict

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

    from constraintlens.domain.models import Event

TraceStatus = Literal["unknown", "open", "closed", "refused"]
Outcome = Literal["ALLOWED", "BLOCKED", "DEGRADED", "OPEN"]


@dataclass
class TraceState:
    trace_id: UUID
    status: TraceStatus = "unknown"
    constraints: list[Constraint] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    verdict_by_key: dict[str, Verdict] = field(default_factory=dict)  # client or default key
    verdict_by_checkpoint: dict[str, Verdict] = field(default_factory=dict)  # (hop, checkpoint)
    regrounds_by_hop: dict[str, int] = field(default_factory=dict)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)  # CHECKPOINT_RECEIVED payloads
    last_event_id: int = 0

    @property
    def outcome(self) -> Outcome:
        if self.status == "open":
            return "OPEN"
        decisions = {v.decision for v in self.verdicts}
        if decisions & {"BLOCK", "ESCALATE"}:
            return "BLOCKED"
        if any(v.degraded for v in self.verdicts):
            return "DEGRADED"
        return "ALLOWED"

    def stored_verdict(self, key: str, triple: str) -> Verdict | None:
        """A verdict already issued for this request, by client key or by checkpoint (§9.6)."""
        return self.verdict_by_key.get(key) or self.verdict_by_checkpoint.get(triple)

    def apply(self, event: Event) -> None:
        self.last_event_id = event.event_id
        p = event.payload
        if event.type == "TRACE_OPENED":
            self.status = "open"
        elif event.type == "CONSTRAINT_REGISTERED":
            self.constraints.append(Constraint.model_validate(p["constraint"]))
        elif event.type == "CHECKPOINT_RECEIVED":
            self.checkpoints.append(p)
        elif event.type == "VERDICT_ISSUED":
            verdict = Verdict.model_validate(p["verdict"])
            self.verdicts.append(verdict)
            self.verdict_by_key[p["idempotency_key"]] = verdict
            triple = default_idempotency_key(verdict.trace_id, verdict.hop_id, verdict.checkpoint)
            self.verdict_by_checkpoint[triple] = verdict
        elif event.type == "REGROUND_ISSUED":
            hop = base_hop_id(event.hop_id or "")
            self.regrounds_by_hop[hop] = self.regrounds_by_hop.get(hop, 0) + 1
        elif event.type == "TRACE_CLOSED":
            self.status = "closed"
        elif event.type == "WARNING" and p.get("refused"):
            self.status = "refused"


def fold(trace_id: UUID, events: Iterable[Event]) -> TraceState:
    state = TraceState(trace_id=trace_id)
    for e in events:
        if e.trace_id == trace_id:
            state.apply(e)
    return state


RETRY_SEPARATOR = "/r"


def retry_hop_id(hop_id: str, attempt: int) -> str:
    """Hop id of the n-th re-grounded retry of a hop (ADR 0005): ``h2`` → ``h2/r1``."""
    if attempt < 1:
        raise ValueError("retry attempts start at 1")
    return f"{base_hop_id(hop_id)}{RETRY_SEPARATOR}{attempt}"


def base_hop_id(hop_id: str) -> str:
    """The hop a retry belongs to; re-ground limits count across all its attempts."""
    return hop_id.split(RETRY_SEPARATOR, 1)[0]


def default_idempotency_key(trace_id: UUID, hop_id: str, checkpoint: str) -> str:
    """§9.6: verdicts are keyed by (trace_id, hop_id, checkpoint)."""
    return f"{trace_id}:{hop_id}:{checkpoint}"
