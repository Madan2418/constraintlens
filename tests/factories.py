"""Builders for valid domain objects, so tests state only what they care about."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from constraintlens.domain.models import (
    Constraint,
    Envelope,
    EventDraft,
    HopMeta,
    Limit,
)

T0 = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make_constraint(**overrides: Any) -> Constraint:
    fields: dict[str, Any] = {
        "constraint_id": uuid4(),
        "scope": "user",
        "owner": "user:alice",
        "subject": "order:4471",
        "action": "refund",
        "resource": "order",
        "limit": Limit(max_value=Decimal(100), unit="USD"),
        "authority": "requires_approval_above_limit",
        "risk_class": "high",
        "origin": "user",
        "canonical_text": "Refund order #4471 up to 100 USD; approval required above 100 USD.",
        "content_hash": sha("constraint"),
        "issued_at": T0,
        "expiry": None,
        "max_age_seconds": 3600,
    }
    fields.update(overrides)
    return Constraint(**fields)


def make_envelope(
    *,
    trace_id: UUID | None = None,
    hop_id: str = "h1",
    parents: list[str] | None = None,
    hop_seq: int = 1,
    agent_id: str = "intake",
    constraints: list[Constraint] | None = None,
    text: str = "Process refund for order #4471, limit $100, approval above.",
    args: dict[str, Any] | None = None,
) -> Envelope:
    return Envelope(
        trace_id=trace_id or uuid4(),
        hop=HopMeta(
            hop_id=hop_id,
            parent_hop_ids=parents or [],
            hop_seq=hop_seq,
            agent_id=agent_id,
            emitted_at=T0,
        ),
        constraints=[make_constraint()] if constraints is None else constraints,
        envelope_digest=sha(f"digest-{hop_id}"),
        payload_text=text,
        payload_args=args,
    )


def make_draft(trace_id: UUID, *, type_: str = "WARNING", hop_id: str | None = None) -> EventDraft:
    return EventDraft(
        trace_id=trace_id,
        type=type_,  # type: ignore[arg-type]
        hop_id=hop_id,
        occurred_at=T0,
        payload={"note": type_},
    )
