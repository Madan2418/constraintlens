"""Trace state as a pure projection of the event log (ARCHITECTURE.md §5, §12.1).

The event log is the single source of truth. Everything the use cases need about a
trace (its registered constraints, status, stored verdicts, re-ground counts, and the
hop DAG the async guard reasons over) is derived here by folding its events. The same
fold drives replay and the trace summary, so they cannot disagree with what actually
happened.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from constraintlens.domain.models import CheckpointRequest, Constraint, Verdict

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime
    from uuid import UUID

    from constraintlens.domain.models import Event

TraceStatus = Literal["unknown", "open", "closed", "refused"]
Outcome = Literal["ALLOWED", "BLOCKED", "DEGRADED", "OPEN"]


@dataclass
class HopHistory:
    """The trace's hop DAG as observed through checkpoints (§9.2, §9.4).

    Built only from recorded checkpoint requests, so the async guard's view of order
    and gaps is itself a projection of the log, and replay sees exactly what the live
    check saw.
    """

    seen: set[str] = field(default_factory=set)  # hops known to exist
    parents: dict[str, set[str]] = field(default_factory=dict)  # DAG edges, child -> parents
    seq: dict[str, int] = field(default_factory=dict)  # hop_seq per emitting hop
    consumed: set[str] = field(default_factory=set)  # envelope hops consumed (not held)
    #: (trace, hop, checkpoint) key -> when it was first held (join timeout, §9.4)
    held_since: dict[str, datetime] = field(default_factory=dict)

    def observe(self, request: CheckpointRequest, *, held: bool) -> None:
        hop = request.envelope.hop
        self.seen.add(hop.hop_id)
        self.parents.setdefault(hop.hop_id, set()).update(hop.parent_hop_ids)
        self.seq.setdefault(hop.hop_id, hop.hop_seq)
        if request.checkpoint in ("PUBLISH", "CONSUME"):
            actor = base_hop_id(request.hop_id)
            self.seen.add(actor)
            if request.checkpoint == "CONSUME" and actor != hop.hop_id:
                self.parents.setdefault(actor, set()).add(hop.hop_id)
        if request.checkpoint == "CONSUME" and not held:
            self.consumed.add(hop.hop_id)

    def observed(self, hop_id: str) -> bool:
        return hop_id in self.seen

    def ancestors(self, hop_id: str) -> set[str]:
        out: set[str] = set()
        stack = list(self.parents.get(hop_id, ()))
        while stack:
            h = stack.pop()
            if h not in out:
                out.add(h)
                stack.extend(self.parents.get(h, ()))
        return out

    def has_consumed_descendant(self, hop_id: str) -> bool:
        """Some envelope downstream of ``hop_id`` was already consumed (§9.4 inversion)."""
        return any(hop_id in self.ancestors(c) for c in self.consumed if c != hop_id)


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
    history: HopHistory = field(default_factory=HopHistory)
    held: list[Verdict] = field(default_factory=list)  # non-final verdicts (§9.4, ADR 0006)
    transit_losses: list[dict[str, Any]] = field(default_factory=list)  # §9.5 (d) sweeper
    #: CHECKPOINT_RECEIVED event id -> the verdict issued for it (final or held).
    verdict_by_checkpoint_event: dict[int, Verdict] = field(default_factory=dict)
    #: (trace, hop, checkpoint) -> fingerprint of the request its final verdict answered
    fingerprint_by_checkpoint: dict[str, str] = field(default_factory=dict)
    conflicts: list[Verdict] = field(default_factory=list)  # duplicates that differed (ADR 0006)
    last_event_id: int = 0
    _open_checkpoint_event: int | None = None
    _open_fingerprint: str | None = None

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
            if event.hop_id:  # the root hop is issued by the service itself (hop_seq 0)
                self.history.seen.add(event.hop_id)
                self.history.seq.setdefault(event.hop_id, 0)
        elif event.type == "CONSTRAINT_REGISTERED":
            self.constraints.append(Constraint.model_validate(p["constraint"]))
        elif event.type == "CHECKPOINT_RECEIVED":
            self.checkpoints.append(p)
            held = bool(p.get("held"))
            if not p.get("conflict"):  # a conflicting duplicate is not trusted with history
                self.history.observe(CheckpointRequest.model_validate(p["request"]), held=held)
            self._open_fingerprint = p.get("fingerprint")
            if held:
                r = p["request"]
                triple = default_idempotency_key(self.trace_id, r["hop_id"], r["checkpoint"])
                self.history.held_since.setdefault(triple, event.occurred_at)
            self._open_checkpoint_event = event.event_id
        elif event.type == "WARNING" and p.get("conflict"):  # may also be held
            verdict = Verdict.model_validate(p["verdict"])
            self.conflicts.append(verdict)
            self._link(verdict)
        elif event.type == "WARNING" and p.get("held"):
            verdict = Verdict.model_validate(p["verdict"])
            self.held.append(verdict)
            self._link(verdict)
        elif event.type == "WARNING" and p.get("transit_loss"):
            self.transit_losses.append(p)
        elif event.type == "VERDICT_ISSUED":
            verdict = Verdict.model_validate(p["verdict"])
            self._link(verdict)
            self.verdicts.append(verdict)
            if self._open_fingerprint is not None:
                triple = default_idempotency_key(
                    verdict.trace_id, verdict.hop_id, verdict.checkpoint
                )
                self.fingerprint_by_checkpoint[triple] = self._open_fingerprint
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

    def _link(self, verdict: Verdict) -> None:
        """A verdict is recorded in the same batch as the checkpoint it answers."""
        if self._open_checkpoint_event is not None:
            self.verdict_by_checkpoint_event[self._open_checkpoint_event] = verdict
            self._open_checkpoint_event = None

    def transit_loss_flagged(self, hop_id: str) -> bool:
        return any(t.get("published_hop_id") == hop_id for t in self.transit_losses)


def request_fingerprint(request: CheckpointRequest) -> str:
    """What makes a duplicate the *same* checkpoint request (§9.6, ADR 0006).

    The constraints it carries (full content), the envelope digest, the hop it consumes
    or emits, and the proposed action. Task text and timestamps are left out on purpose:
    a re-executed agent may reword its task or stamp a new time, and must still get the
    stored verdict. A duplicate whose constraints or action differ is not a redelivery.
    """
    env = request.envelope
    material = {
        "constraints": [c.model_dump(mode="json") for c in env.constraints],
        "digest": env.envelope_digest,
        "hop": [env.hop.hop_id, sorted(env.hop.parent_hop_ids), env.hop.hop_seq],
        "action": (
            request.proposed_action.model_dump(mode="json") if request.proposed_action else None
        ),
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


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
