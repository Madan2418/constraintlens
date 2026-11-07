from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from constraintlens.domain.models import (
    CheckpointRequest,
    Delivery,
    Failure,
    Limit,
    ProposedAction,
    Verdict,
)
from tests.factories import T0, make_constraint, make_draft, make_envelope


def _verdict(**overrides: object) -> Verdict:
    fields: dict[str, object] = {
        "verdict_id": uuid4(),
        "trace_id": uuid4(),
        "hop_id": "h1",
        "checkpoint": "PUBLISH",
        "tier_used": "T0",
        "scores": {},
        "failures": [],
        "decision": "ALLOW",
        "degraded": False,
        "reason": "ok",
        "latency_ms": {"T0": 0.1},
        "cache_hit": False,
        "regrounded_text": None,
    }
    fields.update(overrides)
    return Verdict(**fields)  # type: ignore[arg-type]


class TestConstraint:
    def test_is_frozen(self) -> None:
        c = make_constraint()
        with pytest.raises(ValidationError):
            c.action = "transfer"  # type: ignore[misc]

    def test_limit_is_frozen(self) -> None:
        c = make_constraint()
        assert c.limit is not None
        with pytest.raises(ValidationError):
            c.limit.max_value = Decimal(10_000)  # type: ignore[misc]

    def test_approval_always_needs_no_limit(self) -> None:
        c = make_constraint(authority="requires_approval_always", limit=None)
        assert c.limit is None

    def test_min_only_limit_is_valid(self) -> None:
        assert Limit(max_value=None, min_value=Decimal(1), unit="records").min_value == 1

    def test_expiry_equal_to_issue_rejected(self) -> None:
        with pytest.raises(ValidationError, match="expiry"):
            make_constraint(expiry=T0)

    def test_expiry_after_issue_accepted(self) -> None:
        assert make_constraint(expiry=T0 + timedelta(hours=1)).expiry is not None


class TestEnvelope:
    def test_stripped_envelope_is_representable(self) -> None:
        # Tier 0 must be able to see an envelope with no constraints and report LOST.
        assert make_envelope(constraints=[]).constraints == []

    def test_mutation_is_validated(self) -> None:
        env = make_envelope()
        with pytest.raises(ValidationError):
            env.envelope_digest = "short"

    def test_fan_in_parents(self) -> None:
        env = make_envelope(hop_id="j", parents=["a", "b"], hop_seq=3)
        assert env.hop.parent_hop_ids == ["a", "b"]

    def test_empty_parent_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="empty"):
            make_envelope(hop_id="j", parents=[""])


class TestCheckpointRequest:
    def test_publish_needs_no_action(self) -> None:
        env = make_envelope()
        req = CheckpointRequest(
            trace_id=env.trace_id, hop_id="h1", checkpoint="PUBLISH", envelope=env
        )
        assert req.proposed_action is None

    def test_tool_call_with_action(self) -> None:
        env = make_envelope()
        req = CheckpointRequest(
            trace_id=env.trace_id,
            hop_id="h3",
            checkpoint="TOOL_CALL",
            envelope=env,
            proposed_action=ProposedAction(tool="issue_refund", args={"amount": 2000}),
        )
        assert req.proposed_action is not None
        assert req.proposed_action.args["amount"] == 2000

    def test_empty_tool_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProposedAction(tool="")


class TestVerdict:
    def test_allow_with_warning_may_carry_failures(self) -> None:
        f = Failure(kind="TEXTUAL_DRIFT", constraint_id=uuid4(), detail="low-risk drift")
        v = _verdict(decision="ALLOW_WITH_WARNING", failures=[f])
        assert v.failures == [f]

    def test_escalate_requires_cause(self) -> None:
        with pytest.raises(ValidationError, match="requires a failure"):
            _verdict(decision="ESCALATE")

    def test_degraded_block_needs_no_failure(self) -> None:
        assert _verdict(decision="BLOCK", degraded=True).degraded

    def test_score_bounds_inclusive(self) -> None:
        cid = uuid4()
        assert _verdict(scores={cid: 0.0}).scores[cid] == 0.0
        assert _verdict(scores={cid: 1.0}).scores[cid] == 1.0

    def test_idempotency_key(self) -> None:
        v = _verdict(hop_id="h7", checkpoint="CONSUME")
        assert v.idempotency_key == (v.trace_id, "h7", "CONSUME")


class TestEventAndDelivery:
    def test_draft_with_id(self) -> None:
        tid = uuid4()
        e = make_draft(tid, type_="TRACE_OPENED").with_id(5)
        assert (e.event_id, e.trace_id, e.type) == (5, tid, "TRACE_OPENED")

    def test_draft_rejects_non_positive_id(self) -> None:
        with pytest.raises(ValidationError):
            make_draft(uuid4()).with_id(0)

    def test_delivery_transport_meta(self) -> None:
        d = Delivery(
            stream="tasks",
            message_id="1-0",
            envelope=make_envelope(),
            delivery_count=2,
            enqueued_at=T0,
        )
        meta = d.transport_meta(T0 + timedelta(minutes=12))
        assert meta.delivery_count == 2
        assert meta.consumed_at is not None
        assert meta.enqueued_at is not None
        assert meta.consumed_at - meta.enqueued_at == timedelta(minutes=12)
