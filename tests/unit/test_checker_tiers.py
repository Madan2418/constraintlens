"""Tier 0 (integrity) and Tier 1a (structured action) checks, and checker routing."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from constraintlens.adapters.clock import FrozenClock
from constraintlens.domain.canonical import compute_envelope_digest, issue
from constraintlens.domain.checker.pipeline import CheckContext, TieredChecker, TierOutcome
from constraintlens.domain.checker.tier0_integrity import IntegrityTier
from constraintlens.domain.checker.tier1a_action import ActionTier
from constraintlens.domain.models import (
    ActionBinding,
    CheckpointRequest,
    Constraint,
    ConstraintSpec,
    Envelope,
    HopMeta,
    Limit,
    ProposedAction,
)
from tests.factories import T0

TRACE = UUID(int=7)
REFUND_BINDING = ActionBinding(
    action="refund", resource="order", amount_arg="amount", unit="USD",
    subject_arg="order", subject_kind="order",
)  # fmt: skip


def constraint(**kw: Any) -> Constraint:
    fields: dict[str, Any] = {
        "owner": "user:alice", "action": "refund", "resource": "order",
        "limit": Limit(max_value=Decimal(100), unit="USD"),
        "authority": "requires_approval_above_limit",
    }  # fmt: skip
    fields.update(kw)
    return issue(ConstraintSpec(**fields), constraint_id=uuid4(), issued_at=T0)


def envelope(cs: list[Constraint], *, digest: str | None = None) -> Envelope:
    return Envelope(
        trace_id=TRACE,
        hop=HopMeta(hop_id="h3", parent_hop_ids=["h2"], hop_seq=3, agent_id="pay", emitted_at=T0),
        constraints=cs,
        envelope_digest=digest or compute_envelope_digest(TRACE, cs),
        payload_text="Refund order #4471 in full.",
    )


def request(
    cs: list[Constraint], *, action: ProposedAction | None = None, **kw: Any
) -> CheckpointRequest:
    return CheckpointRequest(
        trace_id=TRACE,
        hop_id="h3",
        checkpoint="TOOL_CALL" if action else "PUBLISH",
        envelope=kw.pop("env", None) or envelope(cs),
        proposed_action=action,
    )


def refund(
    amount: Any, order: Any = 4471, binding: ActionBinding | None = REFUND_BINDING
) -> ProposedAction:
    return ProposedAction(
        tool="issue_refund", args={"order": order, "amount": amount}, binding=binding
    )


async def t0(registered: list[Constraint], req: CheckpointRequest, *, now: Any = T0) -> TierOutcome:
    return await IntegrityTier().run(CheckContext(req, registered, now))


async def t1a(
    registered: list[Constraint], action: ProposedAction, *, now: Any = T0
) -> TierOutcome:
    return await ActionTier().run(CheckContext(request(registered, action=action), registered, now))


def kinds(outcome: TierOutcome) -> list[str]:
    return [f.failure.kind for f in outcome.findings]


class TestTier0:
    async def test_intact_envelope_passes(self) -> None:
        c = constraint()
        out = await t0([c], request([c]))
        assert out.findings == []
        assert not out.terminal

    async def test_lost_dropped_and_extras(self) -> None:
        a, b = constraint(), constraint(scope="org", owner="org:x")
        assert kinds(await t0([a, b], request([]))) == ["CONSTRAINT_LOST"]
        out = await t0([a, b], request([a]))
        assert kinds(out) == ["CONSTRAINT_DROPPED"]
        assert out.findings[0].failure.constraint_id == b.constraint_id
        assert out.terminal
        assert kinds(await t0([a], request([a, b]))) == []  # unregistered extra: no authority

    async def test_tampered_copy_and_stale_hash(self) -> None:
        c = constraint()
        loosened = c.model_copy(update={"limit": Limit(max_value=Decimal(10_000), unit="USD")})
        assert kinds(await t0([c], request([loosened]))) == ["HASH_MISMATCH"]
        # A registered constraint whose stored hash no longer matches its own content.
        broken = c.model_copy(update={"content_hash": "0" * 64})
        assert kinds(await t0([broken], request([broken]))) == ["HASH_MISMATCH"]

    async def test_bad_envelope_digest(self) -> None:
        c = constraint()
        out = await t0([c], request([c], env=envelope([c], digest="f" * 64)))
        assert kinds(out) == ["HASH_MISMATCH"]
        assert out.findings[0].failure.constraint_id is None

    async def test_expired(self) -> None:
        c = constraint(expiry=T0 + timedelta(hours=1))
        assert kinds(await t0([c], request([c]), now=T0 + timedelta(minutes=59))) == []
        assert kinds(await t0([c], request([c]), now=T0 + timedelta(hours=2))) == ["EXPIRED"]


class TestTier1a:
    async def test_applies_only_with_a_binding(self) -> None:
        c = constraint()
        tier = ActionTier()
        assert tier.applies(CheckContext(request([c], action=refund(5)), [c], T0))
        assert not tier.applies(CheckContext(request([c], action=refund(5, binding=None)), [c], T0))
        assert not tier.applies(CheckContext(request([c]), [c], T0))

    async def test_worked_example(self) -> None:
        c = constraint()
        assert (await t1a([c], refund(100))).findings == []
        out = await t1a([c], refund(2000))
        assert kinds(out) == ["ACTION_VIOLATION"]
        assert out.findings[0].requires_approval
        assert "amount=2000 USD" in out.findings[0].failure.detail
        assert out.terminal

    async def test_hard_cap_and_org_policy_named_separately(self) -> None:
        user = constraint()
        org = constraint(
            scope="org",
            owner="org:acme",
            authority="none",
            limit=Limit(max_value=Decimal(50), unit="USD"),
        )
        out = await t1a([user, org], refund(80))
        assert [f.failure.constraint_id for f in out.findings] == [org.constraint_id]
        assert not out.findings[0].requires_approval
        out = await t1a([user, org], refund(2000))
        assert {f.failure.constraint_id for f in out.findings} == {
            user.constraint_id,
            org.constraint_id,
        }

    async def test_subject_scope(self) -> None:
        c = constraint(subject="order:4471")
        assert (await t1a([c], refund(10))).findings == []
        assert (
            "subject=order:4480"
            in (await t1a([c], refund(10, order=4480))).findings[0].failure.detail
        )

    async def test_unrelated_action_is_not_checked(self) -> None:
        c = constraint(action="transfer", resource="funds")
        assert (await t1a([c], refund(10**6))).findings == []

    @pytest.mark.parametrize("bad", [None, "lots", True, "NaN"])
    async def test_unreadable_amount_fails_safe(self, bad: Any) -> None:
        out = await t1a([constraint()], refund(bad))
        assert kinds(out) == ["ACTION_VIOLATION"]
        assert "not a number" in out.findings[0].failure.detail

    async def test_unit_and_subject_args(self) -> None:
        b = ActionBinding(
            action="refund",
            resource="order",
            amount_arg="amount",
            unit_arg="currency",
            subject_arg="order",
        )
        c = constraint(subject="4471")
        ok = ProposedAction(
            tool="r", args={"amount": 5, "currency": "USD", "order": 4471}, binding=b
        )
        assert (await t1a([c], ok)).findings == []
        no_unit = ProposedAction(tool="r", args={"amount": 5, "order": 4471}, binding=b)
        assert "(unit) is missing" in (await t1a([c], no_unit)).findings[0].failure.detail
        no_subject = ProposedAction(tool="r", args={"amount": 5, "currency": "USD"}, binding=b)
        assert "(subject) is missing" in (await t1a([c], no_subject)).findings[0].failure.detail

    async def test_only_the_combination_fails(self) -> None:
        usd = constraint(authority="none", limit=Limit(max_value=Decimal(100), unit="USD"))
        inr = constraint(authority="none", limit=Limit(max_value=Decimal(9000), unit="INR"))
        b = ActionBinding(action="refund", resource="order", amount_arg="amount", unit="INR")
        out = await t1a([usd, inr], ProposedAction(tool="r", args={"amount": 10}, binding=b))
        assert kinds(out) == ["ACTION_VIOLATION"]
        assert all(
            "cannot be compared" in f.failure.detail or "combined" in f.failure.detail
            for f in out.findings
        )

    def test_binding_validation(self) -> None:
        with pytest.raises(ValueError, match="exactly one of unit"):
            ActionBinding(action="a", resource="r", amount_arg="x")
        with pytest.raises(ValueError, match="without amount_arg"):
            ActionBinding(action="a", resource="r", unit="USD")
        with pytest.raises(ValueError, match="subject_kind given"):
            ActionBinding(action="a", resource="r", subject_kind="order")


class TestRouting:
    async def test_terminal_tier_stops_the_chain(self) -> None:
        c = constraint()
        checker = TieredChecker([IntegrityTier(), ActionTier()], FrozenClock())
        lost = await checker.check(CheckContext(request([c], action=refund(5)), [c], T0))
        lost_req = request([], action=refund(5))
        result = await checker.check(CheckContext(lost_req, [c], T0))
        assert result.tiers_run == ["T0"]
        assert result.tier_used == "T0"
        assert lost.tiers_run == ["T0", "T1a"]
        assert set(lost.latency_ms) == {"T0", "T1a"}

    def test_registry_validation(self) -> None:
        with pytest.raises(ValueError, match="at least one tier"):
            TieredChecker([], FrozenClock())
        with pytest.raises(ValueError, match="duplicate"):
            TieredChecker([IntegrityTier(), IntegrityTier()], FrozenClock())
        assert TieredChecker([IntegrityTier()], FrozenClock()).tier_names == ["T0"]
