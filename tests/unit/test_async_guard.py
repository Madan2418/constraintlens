"""Async guard (§7.5, §9): staleness, gaps and holds, inversions, through the use case.

Checkpoints go through ``VerifyCheckpoint`` with a frozen clock, so the hop history the
guard reasons over is exactly what the event log holds.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from constraintlens.adapters.clock import FrozenClock
from constraintlens.adapters.store.memory import InMemoryStore
from constraintlens.application.open_trace import OpenTrace, OpenTraceRequest
from constraintlens.application.trace_queries import ReplayTrace, metrics, summarise
from constraintlens.application.verify_checkpoint import VerifyCheckpoint
from constraintlens.domain.async_guard import GuardConfig, dwell_seconds, readiness
from constraintlens.domain.checker.pipeline import TieredChecker
from constraintlens.domain.checker.tier0_integrity import IntegrityTier
from constraintlens.domain.checker.tier1a_action import ActionTier
from constraintlens.domain.enforcement import Policy
from constraintlens.domain.models import (
    CheckpointRequest,
    Envelope,
    HopMeta,
    TransportMeta,
    Verdict,
)
from constraintlens.domain.projections import HopHistory, fold
from constraintlens.sdk import next_envelope
from tests.factories import T0
from tests.service_helpers import WORKED, ScriptedSemanticTier

GUARD = GuardConfig(dwell_threshold_s=300, join_timeout_s=60)
TEXT_OK = "Refund order #4471, limit $100, approval above."


class Harness:
    def __init__(self, *, semantic: ScriptedSemanticTier | None = None, risk: str = "high") -> None:
        self.store = InMemoryStore()
        self.clock = FrozenClock(T0)
        self.semantic = semantic
        self.risk = risk

    async def open(self) -> Envelope:
        opened = await OpenTrace(store=self.store, clock=self.clock)(
            OpenTraceRequest(WORKED, owner="u", risk_class=self.risk)  # type: ignore[arg-type]
        )
        tiers: list[Any] = [IntegrityTier(), ActionTier()]
        if self.semantic is not None:
            tiers.append(self.semantic)
        self.checker = TieredChecker(tiers, self.clock)
        self.verify = VerifyCheckpoint(
            store=self.store, clock=self.clock, checker=self.checker, policy=Policy(), guard=GUARD
        )
        self.root = opened.envelope
        return opened.envelope

    async def consume(
        self, env: Envelope, hop_id: str, *, dwell_s: float | None = None, delivery: int = 1
    ) -> Verdict:
        meta = None
        if dwell_s is not None:
            now = self.clock.now()
            meta = TransportMeta(
                message_id="1-0", delivery_count=delivery,
                enqueued_at=now - timedelta(seconds=dwell_s), consumed_at=now,
            )  # fmt: skip
        return await self.verify(
            CheckpointRequest(trace_id=env.trace_id, hop_id=hop_id, checkpoint="CONSUME",
                              envelope=env, transport_meta=meta)
        )  # fmt: skip

    async def publish(self, env: Envelope) -> Verdict:
        return await self.verify(
            CheckpointRequest(trace_id=env.trace_id, hop_id=env.hop.hop_id, checkpoint="PUBLISH",
                              envelope=env)
        )  # fmt: skip

    async def state(self) -> Any:
        return fold(self.root.trace_id, await self.store.read_trace(self.root.trace_id))


def emit(parent: Envelope, hop_id: str, text: str = TEXT_OK, **hop: Any) -> Envelope:
    env = next_envelope(parent, hop_id=hop_id, agent_id=f"agent-{hop_id}", text=text, emitted_at=T0)
    if hop:
        env = env.model_copy(update={"hop": env.hop.model_copy(update=hop)})
    return env


def kinds(v: Verdict) -> list[str]:
    return [f.kind for f in v.failures]


# --------------------------------------------------------------------------- normal flow


async def test_linear_chain_has_no_async_findings() -> None:
    h = Harness()
    root = await h.open()
    e1, e2 = emit(root, "h1"), None
    assert (await h.consume(root, "h1")).decision == "ALLOW"
    assert (await h.publish(e1)).decision == "ALLOW"
    assert (await h.consume(e1, "h2", dwell_s=30)).decision == "ALLOW"
    e2 = emit(e1, "h2")
    assert (await h.publish(e2)).decision == "ALLOW"
    assert (await h.consume(e2, "h3")).decision == "ALLOW"


# --------------------------------------------------------------------------- gaps (§9.4)


async def test_gap_holds_until_the_predecessor_is_observed() -> None:
    h = Harness()
    root = await h.open()
    e1 = emit(root, "h1")
    e2 = emit(e1, "h2")  # h1's checkpoints have not reached the service yet

    held = await h.consume(e2, "h3")
    assert held.held
    assert held.decision == "BLOCK"
    assert kinds(held) == ["MISSING_HOP"]
    assert "h1 never observed" in held.failures[0].detail

    # Held is recorded, not stored: a redelivery is checked again, not answered from cache.
    again = await h.consume(e2, "h3", delivery=2)
    assert again.held
    assert again.verdict_id != held.verdict_id
    state = await h.state()
    assert [v.verdict_id for v in state.held] == [held.verdict_id, again.verdict_id]
    assert state.verdicts == []

    # The delayed predecessor arrives; the next redelivery proceeds and is final.
    await h.consume(root, "h1")
    await h.publish(e1)
    final = await h.consume(e2, "h3", delivery=3)
    assert not final.held
    assert final.decision == "ALLOW"
    assert await h.consume(e2, "h3", delivery=4) == final  # now idempotent


async def test_gap_proceeds_with_a_warning_after_the_join_timeout() -> None:
    h = Harness()
    root = await h.open()
    e2 = emit(emit(root, "h1"), "h2")
    assert (await h.consume(e2, "h3")).held
    h.clock.advance(30)
    assert (await h.consume(e2, "h3")).held  # waited 30 s of 60
    h.clock.advance(31)
    late = await h.consume(e2, "h3")
    assert not late.held
    assert late.decision == "ALLOW_WITH_WARNING"
    assert "join timeout of 60s elapsed" in late.failures[0].detail


async def test_timeout_counts_from_the_first_hold_not_from_the_envelope() -> None:
    """A producer cannot backdate ``emitted_at`` to skip the hold."""
    h = Harness()
    root = await h.open()
    e2 = emit(emit(root, "h1"), "h2", emitted_at=T0 - timedelta(days=1))
    assert (await h.consume(e2, "h3")).held


async def test_low_risk_gap_is_flagged_not_held() -> None:
    h = Harness(risk="low")
    root = await h.open()
    v = await h.consume(emit(emit(root, "h1"), "h2"), "h3")
    assert not v.held
    assert v.decision == "ALLOW_WITH_WARNING"
    assert "low-risk constraints only" in v.failures[0].detail


async def test_hop_seq_gap_is_a_missing_hop() -> None:
    h = Harness()
    root = await h.open()
    e1 = emit(root, "h1")
    await h.consume(root, "h1")
    await h.publish(e1)
    skipped = emit(e1, "h4", hop_seq=4)  # parent h1 has seq 1: hops 2 and 3 are missing
    v = await h.consume(skipped, "h5")
    assert v.held
    assert "2 hop(s) missing" in v.failures[0].detail


async def test_fan_in_join_waits_for_every_parent() -> None:
    h = Harness()
    root = await h.open()
    a, b = emit(root, "ha"), emit(root, "hb")
    await h.consume(root, "ha")
    await h.publish(a)
    join = emit(a, "hj").model_copy(
        update={"hop": HopMeta(hop_id="hj", parent_hop_ids=["ha", "hb"], hop_seq=2,
                               agent_id="joiner", emitted_at=T0)}
    )  # fmt: skip
    assert (await h.consume(join, "hk")).held  # hb not observed yet
    await h.consume(root, "hb")
    await h.publish(b)
    assert (await h.consume(join, "hk")).decision == "ALLOW"


# --------------------------------------------------------------------------- inversions


async def test_late_delivery_is_flagged_and_still_checked() -> None:
    h = Harness()
    root = await h.open()
    e1 = emit(root, "h1")
    e2 = emit(e1, "h2")
    for step in (h.consume(root, "h1"), h.publish(e1), h.consume(e1, "h2"), h.publish(e2)):
        await step
    assert (await h.consume(e2, "h3")).decision == "ALLOW"

    # A second copy of h1's task arrives after h3 already consumed downstream work.
    late = await h.consume(e1, "h2-late")
    assert kinds(late) == ["OUT_OF_ORDER"]
    assert late.decision == "ALLOW_WITH_WARNING"
    assert "already consumed" in late.failures[0].detail
    # The frontier never moves backwards: the history still knows h3 consumed e2.
    assert "h2" in (await h.state()).history.consumed


async def test_hop_seq_not_after_parent_is_out_of_order() -> None:
    h = Harness()
    root = await h.open()
    e1 = emit(root, "h1")
    await h.consume(root, "h1")
    await h.publish(e1)
    v = await h.consume(emit(e1, "h2", hop_seq=1), "h3")
    assert kinds(v) == ["OUT_OF_ORDER"]
    assert "not after its parents" in v.failures[0].detail


async def test_out_of_order_does_not_hide_an_action_violation() -> None:
    """Checked, not trusted: a late delivery still goes through the later tiers."""
    from constraintlens.domain.models import ActionBinding, ProposedAction

    h = Harness()
    root = await h.open()
    e1 = emit(root, "h1")
    await h.consume(root, "h1")
    await h.consume(e1, "h2")
    binding = ActionBinding(action="refund", resource="order", amount_arg="amount", unit="USD")
    v = await h.verify(
        CheckpointRequest(
            trace_id=root.trace_id, hop_id="h2.t1", checkpoint="TOOL_CALL", envelope=e1,
            proposed_action=ProposedAction(tool="issue_refund", args={"amount": 5000},
                                           binding=binding),
        )
    )  # fmt: skip
    assert v.decision == "BLOCK"
    assert kinds(v) == ["ACTION_VIOLATION"]


# --------------------------------------------------------------------------- staleness


@pytest.mark.parametrize(
    ("risk", "semantic", "decision", "degraded", "kinds_"),
    [
        ("high", None, "BLOCK", True, []),  # cannot re-verify: fail closed
        ("low", None, "ALLOW_WITH_WARNING", True, []),  # cannot re-verify: fail open
        ("high", ScriptedSemanticTier(), "ALLOW", False, []),  # re-verified
        ("high", ScriptedSemanticTier("$999"), "BLOCK", False, ["TEXTUAL_DRIFT", "STALE"]),
        ("low", ScriptedSemanticTier("$999"), "RE_GROUND", False, ["TEXTUAL_DRIFT", "STALE"]),
        ("high", ScriptedSemanticTier(available=False), "BLOCK", True, []),
    ],
)
async def test_stale_queue_dwell_demands_reverification(
    risk: str, semantic: ScriptedSemanticTier | None, decision: str, degraded: bool,
    kinds_: list[str],
) -> None:  # fmt: skip
    h = Harness(semantic=semantic, risk=risk)
    root = await h.open()
    await h.consume(root, "h1")
    e1 = emit(root, "h1")
    await h.publish(e1)
    v = await h.consume(e1, "h2", dwell_s=12 * 60)  # the §2.1 12-minute dwell
    assert (v.decision, v.degraded, kinds(v)) == (decision, degraded, kinds_)
    if semantic is not None and semantic.available and semantic.keyword == "$100":
        assert "re-verified" in v.reason
    if not semantic or not semantic.available:
        assert "no semantic tier could re-verify" in v.reason


async def test_constraint_older_than_max_age_is_stale() -> None:
    h = Harness()
    root = await h.open()
    await h.consume(root, "h1")
    h.clock.advance(3601)  # default max_age is 3600 s
    v = await h.consume(emit(root, "h1"), "h2")
    assert v.degraded
    assert v.decision == "BLOCK"
    assert "exceeds max_age 3600s" in v.reason


async def test_staleness_only_applies_at_consume() -> None:
    h = Harness()
    root = await h.open()
    h.clock.advance(3601)
    assert (await h.publish(emit(root, "h1"))).decision == "ALLOW"


async def test_terminal_integrity_failure_makes_staleness_moot() -> None:
    h = Harness()
    root = await h.open()
    stripped = emit(root, "h1").model_copy(update={"constraints": []})
    v = await h.consume(stripped, "h2", dwell_s=3600)
    assert kinds(v) == ["CONSTRAINT_LOST"]
    assert not v.degraded


def test_dwell_and_readiness_helpers() -> None:
    env = emit(Envelope.model_validate(_root_json()), "h1")
    req = CheckpointRequest(trace_id=env.trace_id, hop_id="h2", checkpoint="CONSUME",
                            envelope=env, transport_meta=TransportMeta(enqueued_at=T0))  # fmt: skip
    assert dwell_seconds(req, T0 + timedelta(seconds=90)) == 90  # consumed_at defaults to now
    assert dwell_seconds(req.model_copy(update={"transport_meta": None}), T0) is None
    skewed = TransportMeta(enqueued_at=T0, consumed_at=T0 - timedelta(seconds=5))
    assert dwell_seconds(req.model_copy(update={"transport_meta": skewed}), T0) == 0
    publish = req.model_copy(update={"checkpoint": "PUBLISH"})
    assert readiness(publish, HopHistory(), T0, GUARD, key="k").ready


def _root_json() -> dict[str, Any]:
    from tests.factories import make_envelope

    return make_envelope(hop_id="h0", hop_seq=0).model_dump(mode="json")


# --------------------------------------------------------------------------- replay & views


async def test_replay_reproduces_holds_inversions_and_staleness_exactly() -> None:
    h = Harness()
    root = await h.open()
    e1 = emit(root, "h1")
    e2 = emit(e1, "h2")
    await h.consume(e2, "h3")  # held: h1 unseen
    await h.consume(root, "h1")
    await h.publish(e1)
    await h.consume(e1, "h2", dwell_s=900)  # stale -> degraded BLOCK
    await h.consume(e2, "h3")  # released: ALLOW
    await h.consume(root, "h1-late")  # late delivery: OUT_OF_ORDER

    items = await ReplayTrace(store=h.store, guard=GUARD)(
        root.trace_id, checker=h.checker, policy=Policy()
    )
    assert [(i.hop_id, i.original_decision, i.original_held) for i in items] == [
        ("h3", "BLOCK", True), ("h1", "ALLOW", False), ("h1", "ALLOW", False),
        ("h2", "BLOCK", False), ("h3", "ALLOW", False), ("h1-late", "ALLOW_WITH_WARNING", False),
    ]  # fmt: skip
    assert not any(i.changed for i in items)

    summary = summarise(await h.state())
    assert [v["hop_id"] for v in summary["held"]] == ["h3"]
    m = await metrics(h.store)
    assert m["held"] == 1


async def test_held_events_are_recorded_but_not_final() -> None:
    h = Harness()
    root = await h.open()
    await h.consume(emit(emit(root, "h1"), "h2"), "h3")
    events = await h.store.read_trace(root.trace_id)
    tail = [(e.type, e.payload.get("held"), e.payload.get("final")) for e in events[-4:]]
    assert tail == [
        ("CHECKPOINT_RECEIVED", True, None), ("TIER_EXECUTED", None, None),
        ("WARNING", True, None), ("DECISION_ENFORCED", None, False),
    ]  # fmt: skip
    tier = events[-3].payload
    assert tier["tier"] == "T0"
    assert tier["failures"][0]["kind"] == "MISSING_HOP"


async def test_findings_are_recorded_against_the_tier_that_produced_them() -> None:
    h = Harness(semantic=ScriptedSemanticTier("$999"))
    root = await h.open()
    e1 = emit(root, "h1")
    await h.consume(root, "h1")
    await h.consume(e1, "h2-late-prep")
    await h.consume(root, "h1-late")  # OUT_OF_ORDER (T0) and TEXTUAL_DRIFT (T1c)
    events = await h.store.read_trace(root.trace_id)
    tiers = {e.payload["tier"]: [f["kind"] for f in e.payload["failures"]]
             for e in events[-5:] if e.type == "TIER_EXECUTED"}  # fmt: skip
    assert tiers == {"T0": ["OUT_OF_ORDER"], "T1c": ["TEXTUAL_DRIFT"]}


def test_held_verdict_postcondition() -> None:
    from uuid import uuid4

    with pytest.raises(ValueError, match="held verdict must BLOCK"):
        Verdict(
            verdict_id=uuid4(), trace_id=UUID(int=1), hop_id="h", checkpoint="CONSUME",
            tier_used="T0", scores={}, failures=[], decision="ALLOW", degraded=False,
            reason="r", latency_ms={}, cache_hit=False, regrounded_text=None, held=True,
        )  # fmt: skip


# --------------------------------------------------------------------------- conflicts


async def test_duplicate_with_a_different_action_is_checked_afresh() -> None:
    """Same key, different request: not a redelivery (ADR 0006)."""
    from constraintlens.domain.models import ActionBinding, ProposedAction

    h = Harness()
    root = await h.open()
    binding = ActionBinding(action="refund", resource="order", amount_arg="amount", unit="USD")

    def tool(amount: int, text: str = TEXT_OK, at: Any = T0) -> CheckpointRequest:
        env = emit(root, "h1", text=text).model_copy(
            update={"hop": emit(root, "h1").hop.model_copy(update={"emitted_at": at})}
        )
        return CheckpointRequest(
            trace_id=root.trace_id, hop_id="h1.t1", checkpoint="TOOL_CALL", envelope=env,
            proposed_action=ProposedAction(tool="issue_refund", args={"amount": amount},
                                           binding=binding),
        )  # fmt: skip

    allowed = await h.verify(tool(50))
    assert allowed.decision == "ALLOW"
    # A re-executed agent rewording its text at a later time is still a redelivery.
    reworded = await h.verify(tool(50, text="Refund it, max $100.", at=T0 + timedelta(hours=1)))
    assert reworded == allowed
    # The same sub-hop proposing a different amount is not: it is checked on its own.
    changed = await h.verify(tool(2000))
    assert changed.decision == "BLOCK"
    assert changed.verdict_id != allowed.verdict_id
    state = await h.state()
    assert [v.verdict_id for v in state.verdicts] == [allowed.verdict_id]  # still the answer
    assert [v.verdict_id for v in state.conflicts] == [changed.verdict_id]
    assert summarise(state)["conflicts"][0]["decision"] == "BLOCK"
    items = await ReplayTrace(store=h.store, guard=GUARD)(
        root.trace_id, checker=h.checker, policy=Policy()
    )
    assert [(i.original_decision, i.changed) for i in items] == [
        ("ALLOW", False), ("BLOCK", False),
    ]  # fmt: skip
