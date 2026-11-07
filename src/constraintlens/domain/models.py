"""Domain data models (ARCHITECTURE.md §8).

This module is the source of truth for every structure that crosses a boundary.
All models are Pydantic v2 with ``extra="forbid"``; immutable ones are frozen.
Validators encode the invariants the spec states, so the domain core never has
to handle malformed data ("make illegal states unrepresentable", §5).

Pure module: imports only the standard library and pydantic.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from constraintlens.domain.subject import SubjectScope

if TYPE_CHECKING:
    from datetime import datetime

# --------------------------------------------------------------------------- #
# Vocabulary (closed sets from §4, §6.4, §7.4, §7.7, §7.8, §8)
# --------------------------------------------------------------------------- #

Scope = Literal["user", "org", "system"]
Authority = Literal["requires_approval_above_limit", "requires_approval_always", "none"]
RiskClass = Literal["high", "low"]
Origin = Literal["user", "upstream_agent", "policy"]

Checkpoint = Literal["PUBLISH", "CONSUME", "TOOL_CALL"]
TierId = Literal["T0", "T1a", "T1b", "T1c", "T2"]
Decision = Literal["ALLOW", "ALLOW_WITH_WARNING", "RE_GROUND", "BLOCK", "ESCALATE"]

FailureKind = Literal[
    "ACTION_VIOLATION",
    "TEXTUAL_DRIFT",
    "CONSTRAINT_LOST",
    "CONSTRAINT_DROPPED",
    "HASH_MISMATCH",
    "EXPIRED",
    "STALE",
    "OUT_OF_ORDER",
    "MISSING_HOP",
    "UNSATISFIABLE",
]

EventType = Literal[
    "TRACE_OPENED",
    "CONSTRAINT_REGISTERED",
    "CHECKPOINT_RECEIVED",
    "TIER_EXECUTED",
    "VERDICT_ISSUED",
    "DECISION_ENFORCED",
    "REGROUND_ISSUED",
    "ATTRIBUTION_COMPUTED",
    "TRACE_CLOSED",
    "JUDGE_FAILED",
    "WARNING",
]

JudgeLabel = Literal["ENTAILED", "CONTRADICTED", "AMBIGUOUS"]
AttributionCause = Literal["REWRITE", "STALE_CONTEXT", "TRANSIT_LOSS", "INJECTION", "UNKNOWN"]

BLOCKING_DECISIONS: frozenset[str] = frozenset({"RE_GROUND", "BLOCK", "ESCALATE"})

_SHA256_HEX = r"^[0-9a-f]{64}$"

NonEmptyStr = Field(min_length=1)


class _Model(BaseModel):
    """Mutable base: unknown fields rejected."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class _Frozen(BaseModel):
    """Immutable base: unknown fields rejected, no mutation after construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- #
# §8.1 Constraint
# --------------------------------------------------------------------------- #


class Limit(_Frozen):
    """A numeric bound on the constrained action. At least one bound is required."""

    max_value: Decimal | None
    min_value: Decimal | None = None
    unit: str = NonEmptyStr  # "USD", "INR", "records", ...

    @model_validator(mode="after")
    def _bounds_consistent(self) -> Limit:
        if self.max_value is None and self.min_value is None:
            raise ValueError("a Limit needs max_value, min_value, or both")
        if (
            self.max_value is not None
            and self.min_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError(f"min_value {self.min_value} exceeds max_value {self.max_value}")
        return self


def _check_restriction(subject: str, authority: str, limit: Limit | None) -> None:
    """Invariants shared by ``ConstraintSpec`` and ``Constraint``."""
    SubjectScope.parse(subject)  # raises ValueError on a malformed subject (ADR 0003)
    if authority == "requires_approval_above_limit" and (limit is None or limit.max_value is None):
        raise ValueError("authority 'requires_approval_above_limit' needs a limit with max_value")


class ConstraintSpec(_Frozen):
    """A constraint before issue: what extraction produces or a policy declares (§7.1).

    ``issue()`` in ``domain/canonical.py`` turns it into a ``Constraint`` by adding the
    id, issue time, canonical text and content hash.
    """

    scope: Scope = "user"
    owner: str = NonEmptyStr
    subject: str = "*"
    action: str = NonEmptyStr
    resource: str = NonEmptyStr
    limit: Limit | None = None
    authority: Authority = "none"
    risk_class: RiskClass = "high"
    origin: Origin = "user"
    expiry: AwareDatetime | None = None
    max_age_seconds: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _consistent(self) -> ConstraintSpec:
        _check_restriction(self.subject, self.authority, self.limit)
        return self


class Constraint(_Frozen):
    """A structured restriction attached to a task (§4.1). Read-only after issue (§7.2)."""

    constraint_id: UUID
    scope: Scope
    owner: str = NonEmptyStr  # principal that issued it
    subject: str = NonEmptyStr  # who/what it applies to
    action: str = NonEmptyStr  # "refund" | "transfer" | "delete" | ...
    resource: str = NonEmptyStr  # "order" | "customer_account" | ...
    limit: Limit | None
    authority: Authority
    risk_class: RiskClass = "high"
    origin: Origin
    canonical_text: str = NonEmptyStr
    content_hash: str = Field(pattern=_SHA256_HEX)  # SHA-256 over canonical JSON
    issued_at: AwareDatetime
    expiry: AwareDatetime | None
    max_age_seconds: int | None = Field(default=None, gt=0)  # freshness bound for async hops

    @model_validator(mode="after")
    def _consistent(self) -> Constraint:
        if self.expiry is not None and self.expiry <= self.issued_at:
            raise ValueError("expiry must be later than issued_at")
        _check_restriction(self.subject, self.authority, self.limit)
        return self

    def spec(self) -> ConstraintSpec:
        """The issuable fields of this constraint (inverse of ``issue``)."""
        return ConstraintSpec.model_validate(
            self.model_dump(include=set(ConstraintSpec.model_fields))
        )


# --------------------------------------------------------------------------- #
# §8.2 Envelope and Hop
# --------------------------------------------------------------------------- #


class HopMeta(_Frozen):
    """Identity of one agent step (§4.1, §9.2)."""

    hop_id: str = NonEmptyStr
    parent_hop_ids: list[str]
    hop_seq: int = Field(ge=0)  # monotonic per trace path
    agent_id: str = NonEmptyStr
    emitted_at: AwareDatetime

    @model_validator(mode="after")
    def _dag_edges_valid(self) -> HopMeta:
        if self.hop_id in self.parent_hop_ids:
            raise ValueError("a hop cannot be its own parent")
        if len(set(self.parent_hop_ids)) != len(self.parent_hop_ids):
            raise ValueError("parent_hop_ids contains duplicates")
        if any(not p for p in self.parent_hop_ids):
            raise ValueError("parent_hop_ids contains an empty id")
        return self


class Envelope(_Model):
    """Metadata carried beside the natural-language payload (§7.2).

    ``constraints`` may be empty: a stripped envelope must be representable so
    Tier 0 can report ``CONSTRAINT_LOST`` instead of the request being rejected.
    """

    trace_id: UUID
    hop: HopMeta
    constraints: list[Constraint]
    envelope_digest: str = Field(pattern=_SHA256_HEX)
    payload_text: str  # T_k
    payload_args: dict[str, Any] | None = None  # structured args if available

    @model_validator(mode="after")
    def _unique_constraints(self) -> Envelope:
        ids = [c.constraint_id for c in self.constraints]
        if len(set(ids)) != len(ids):
            raise ValueError("envelope carries duplicate constraint_id values")
        return self


# --------------------------------------------------------------------------- #
# §8.3 Checkpoint request and verdict
# --------------------------------------------------------------------------- #


class ProposedAction(_Frozen):
    """A_k: the structured tool call a hop is about to execute (§4.1, §13.5)."""

    tool: str = NonEmptyStr
    args: dict[str, Any] = Field(default_factory=dict)


class TransportMeta(_Frozen):
    """Delivery metadata for queue-borne checkpoints (§9.2)."""

    message_id: str | None = None
    delivery_count: int = Field(default=1, ge=1)
    enqueued_at: AwareDatetime | None = None
    consumed_at: AwareDatetime | None = None


class CheckpointRequest(_Model):
    trace_id: UUID
    hop_id: str = NonEmptyStr
    checkpoint: Checkpoint
    envelope: Envelope
    proposed_action: ProposedAction | None = None  # required at TOOL_CALL
    transport_meta: TransportMeta | None = None  # delivery count, enqueue time

    @model_validator(mode="after")
    def _consistent(self) -> CheckpointRequest:
        if self.checkpoint == "TOOL_CALL" and self.proposed_action is None:
            raise ValueError("proposed_action is required at the TOOL_CALL checkpoint")
        if self.envelope.trace_id != self.trace_id:
            raise ValueError("envelope.trace_id does not match the request trace_id")
        return self


class Failure(_Frozen):
    kind: FailureKind
    constraint_id: UUID | None
    detail: str = NonEmptyStr


class Verdict(_Frozen):
    """Outcome of one checkpoint (§4.3). Idempotent on (trace_id, hop_id, checkpoint).

    Postconditions (design by contract, §5): every verdict names its tier, carries a
    reason, keeps scores in [0, 1], and never blocks without a stated cause.
    """

    verdict_id: UUID
    trace_id: UUID
    hop_id: str = NonEmptyStr
    checkpoint: Checkpoint
    tier_used: TierId
    scores: dict[UUID, float]  # per-constraint entailment
    failures: list[Failure]
    decision: Decision
    degraded: bool  # a tier was skipped or failed
    reason: str = NonEmptyStr
    latency_ms: dict[str, float]  # per tier
    cache_hit: bool
    regrounded_text: str | None

    @model_validator(mode="after")
    def _postconditions(self) -> Verdict:
        for cid, score in self.scores.items():
            if not 0.0 <= score <= 1.0:
                raise ValueError(f"score for {cid} is {score}; must lie in [0, 1]")
        for tier, ms in self.latency_ms.items():
            if ms < 0:
                raise ValueError(f"latency for {tier} is negative")
        if self.decision in BLOCKING_DECISIONS and not self.failures and not self.degraded:
            raise ValueError(f"decision {self.decision} requires a failure or a degraded verdict")
        if self.decision == "RE_GROUND" and not self.regrounded_text:
            raise ValueError("RE_GROUND requires regrounded_text")
        return self

    @property
    def idempotency_key(self) -> tuple[UUID, str, str]:
        return (self.trace_id, self.hop_id, self.checkpoint)


# --------------------------------------------------------------------------- #
# §8.4 Event
# --------------------------------------------------------------------------- #


class EventDraft(_Frozen):
    """An event before the store assigns its sequence number."""

    trace_id: UUID
    type: EventType
    hop_id: str | None = None
    occurred_at: AwareDatetime
    payload: dict[str, Any] = Field(default_factory=dict)

    def with_id(self, event_id: int) -> Event:
        return Event(event_id=event_id, **self.model_dump())


class Event(_Frozen):
    """An entry in the append-only log, the single source of truth (§12.1)."""

    event_id: int = Field(ge=1)  # monotonic sequence (store-assigned)
    trace_id: UUID
    type: EventType
    hop_id: str | None
    occurred_at: AwareDatetime
    payload: dict[str, Any]


# --------------------------------------------------------------------------- #
# Port-facing value objects (§6.3, §7.4, §7.7, §9.1)
# --------------------------------------------------------------------------- #


class JudgeResult(_Frozen):
    """Strict judge output contract: ``{verdict, reason, confidence}`` (§7.4)."""

    verdict: JudgeLabel
    reason: str = NonEmptyStr
    confidence: float = Field(ge=0.0, le=1.0)


class Delivery(_Frozen):
    """One delivery of an envelope from a queue (at-least-once, §9.1)."""

    stream: str = NonEmptyStr
    message_id: str = NonEmptyStr
    envelope: Envelope
    delivery_count: int = Field(ge=1)
    enqueued_at: AwareDatetime

    def transport_meta(self, consumed_at: datetime) -> TransportMeta:
        return TransportMeta(
            message_id=self.message_id,
            delivery_count=self.delivery_count,
            enqueued_at=self.enqueued_at,
            consumed_at=consumed_at,
        )


class Attribution(_Frozen):
    """Culprit hop for one constraint (§7.7 step 5)."""

    constraint_id: UUID
    culprit_hop: str | None
    culprit_agent: str | None
    cause: AttributionCause
    delta: float
    evidence_events: list[int]
