"""VerifyCheckpoint, CloseTrace, ReplayTrace and metrics (application layer)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from constraintlens.adapters.clock import FrozenClock
from constraintlens.adapters.store.memory import InMemoryStore
from constraintlens.application.open_trace import OpenTrace, OpenTraceRequest
from constraintlens.application.trace_queries import CloseTrace, ReplayTrace, metrics, summarise
from constraintlens.application.verify_checkpoint import VerifyCheckpoint
from constraintlens.domain.checker.pipeline import TieredChecker
from constraintlens.domain.checker.tier0_integrity import IntegrityTier
from constraintlens.domain.checker.tier1a_action import ActionTier
from constraintlens.domain.enforcement import Policy
from constraintlens.domain.errors import InvalidEnvelope, UnknownTrace
from constraintlens.domain.models import (
    ActionBinding,
    CheckpointRequest,
    Envelope,
    EventDraft,
    ProposedAction,
)
from constraintlens.domain.projections import retry_hop_id
from constraintlens.sdk import next_envelope
from tests.factories import T0
from tests.service_helpers import WORKED, KeywordDriftTier

BINDING = ActionBinding(action="refund", resource="order", amount_arg="amount", unit="USD")


class FailingAppendStore(InMemoryStore):
    """Accepts the trace opening, then the recorder fails."""

    fail = False

    async def append(self, drafts: Any) -> Any:
        if self.fail:
            raise OSError("disk full")
        return await super().append(drafts)


async def setup(
    store: InMemoryStore | None = None, *, drift: bool = False, policy: Policy | None = None,
    text: str = WORKED,
) -> tuple[InMemoryStore, VerifyCheckpoint, Envelope]:  # fmt: skip
    store = store or InMemoryStore()
    clock = FrozenClock(T0)
    opened = await OpenTrace(store=store, clock=clock)(OpenTraceRequest(text, owner="u"))
    tiers: list[Any] = [IntegrityTier(), ActionTier()] + ([KeywordDriftTier()] if drift else [])
    verify = VerifyCheckpoint(
        store=store, clock=clock, checker=TieredChecker(tiers, clock), policy=policy or Policy()
    )
    return store, verify, opened.envelope


def tool_request(root: Envelope, amount: int, hop_id: str = "h3.t1") -> CheckpointRequest:
    env = next_envelope(root, hop_id="h3", agent_id="payments", text="Refund in full.",
                        emitted_at=T0)  # fmt: skip
    return CheckpointRequest(
        trace_id=root.trace_id, hop_id=hop_id, checkpoint="TOOL_CALL", envelope=env,
        proposed_action=ProposedAction(
            tool="issue_refund", args={"amount": amount}, binding=BINDING
        ),
    )  # fmt: skip


def publish_request(root: Envelope, text: str, hop_id: str = "h2") -> CheckpointRequest:
    env = next_envelope(root, hop_id=hop_id, agent_id="triage", text=text, emitted_at=T0)
    return CheckpointRequest(
        trace_id=root.trace_id, hop_id=hop_id, checkpoint="PUBLISH", envelope=env
    )


async def test_blocks_and_records_everything() -> None:
    store, verify, root = await setup()
    verdict = await verify(tool_request(root, 2000))
    assert (verdict.decision, verdict.tier_used) == ("BLOCK", "T1a")
    types = [e.type for e in await store.read_trace(root.trace_id)]
    assert types[-5:] == [
        "CHECKPOINT_RECEIVED", "TIER_EXECUTED", "TIER_EXECUTED", "VERDICT_ISSUED",
        "DECISION_ENFORCED",
    ]  # fmt: skip


async def test_redelivery_returns_the_stored_verdict() -> None:
    store, verify, root = await setup()
    first = await verify(tool_request(root, 2000))
    again = await verify(tool_request(root, 2000))
    via_new_key = await verify(tool_request(root, 2000), idempotency_key="client-key-1")
    assert first == again == via_new_key
    events = await store.read_trace(root.trace_id)
    assert [e.type for e in events].count("VERDICT_ISSUED") == 1
    assert [e.payload.get("redelivery") for e in events if e.type == "WARNING"] == [True, True]


async def test_concurrent_duplicates_never_conflict() -> None:
    store, verify, root = await setup()
    verdicts = await asyncio.gather(*(verify(tool_request(root, 2000)) for _ in range(10)))
    assert len({v.verdict_id for v in verdicts}) == 1
    events = await store.read_trace(root.trace_id)
    assert [e.type for e in events].count("VERDICT_ISSUED") == 1


async def test_unknown_closed_and_refused_traces() -> None:
    store, verify, root = await setup()
    other = root.model_copy(update={"trace_id": root.trace_id.__class__(int=99)})
    with pytest.raises(UnknownTrace):
        await verify(tool_request(other, 1))
    await CloseTrace(store=store, clock=FrozenClock(T0))(root.trace_id)
    with pytest.raises(InvalidEnvelope, match="closed"):
        await verify(tool_request(root, 1))


async def test_recorder_failure_fails_closed_for_high_risk() -> None:
    store = FailingAppendStore()
    _, verify, root = await setup(store)
    store.fail = True
    verdict = await verify(tool_request(root, 5))  # would be ALLOW
    assert verdict.decision == "BLOCK"
    assert verdict.degraded
    assert "not recorded" in verdict.reason


async def test_recorder_failure_fails_open_for_low_risk() -> None:
    store = FailingAppendStore()
    _, verify, root = await setup(store, policy=Policy(fail_high_risk="open"))
    store.fail = True
    verdict = await verify(tool_request(root, 5))
    assert (verdict.decision, verdict.degraded) == ("ALLOW_WITH_WARNING", True)


async def test_task_text_is_redacted_before_persistence() -> None:
    store, verify, root = await setup(drift=False)
    await verify(publish_request(root, "Refund $100 to ana@example.com"))
    received = [e for e in await store.read_trace(root.trace_id) if e.type == "CHECKPOINT_RECEIVED"]
    assert received[0].payload["request"]["envelope"]["payload_text"] == "Refund $100 to [EMAIL]"


async def test_reground_then_block_after_limit() -> None:
    store, verify, root = await setup(drift=True, policy=Policy(max_regrounds=2))
    first = await verify(publish_request(root, "Resolve with a refund."))
    assert first.decision == "RE_GROUND"
    assert first.regrounded_text is not None
    assert "100 USD" in first.regrounded_text
    second = await verify(publish_request(root, "Resolve it.", hop_id=retry_hop_id("h2", 1)))
    assert second.decision == "RE_GROUND"
    third = await verify(publish_request(root, "Resolve now.", hop_id=retry_hop_id("h2", 2)))
    assert third.decision == "BLOCK"
    fixed = await verify(publish_request(root, "Refund up to $100.", hop_id="h9"))
    assert fixed.decision == "ALLOW"
    types = [e.type for e in await store.read_trace(root.trace_id)]
    assert types.count("REGROUND_ISSUED") == 2


async def test_close_summary_and_replay() -> None:
    store, verify, root = await setup()
    await verify(tool_request(root, 50, hop_id="h3.t1"))
    await verify(tool_request(root, 2000, hop_id="h3.t2"))
    closed = await CloseTrace(store=store, clock=FrozenClock(T0))(root.trace_id)
    assert (closed.status, closed.outcome) == ("closed", "BLOCKED")
    again = await CloseTrace(store=store, clock=FrozenClock(T0))(root.trace_id)  # idempotent
    assert again.last_event_id == closed.last_event_id
    summary = summarise(closed)
    assert [v["decision"] for v in summary["verdicts"]] == ["ALLOW", "BLOCK"]

    clock = FrozenClock(T0)
    replay = ReplayTrace(store=store)
    full = TieredChecker([IntegrityTier(), ActionTier()], clock)
    t0_only = TieredChecker([IntegrityTier()], clock)
    same = await replay(root.trace_id, checker=full, policy=Policy())  # fmt: skip
    assert [i.replayed_decision for i in same] == ["ALLOW", "BLOCK"]
    assert not any(i.changed for i in same)
    ablated = await replay(root.trace_id, checker=t0_only, policy=Policy())
    assert [i.changed for i in ablated] == [False, True]
    escalating = await replay(root.trace_id, checker=full, policy=Policy(escalate_approvals=True))
    assert escalating[1].replayed_decision == "ESCALATE"
    with pytest.raises(UnknownTrace):
        await replay(root.trace_id.__class__(int=5), checker=t0_only, policy=Policy())  # fmt: skip
    with pytest.raises(UnknownTrace):
        await CloseTrace(store=store, clock=clock)(root.trace_id.__class__(int=5))


async def test_metrics() -> None:
    store, verify, root = await setup()
    await verify(tool_request(root, 50, hop_id="h3.t1"))
    await verify(tool_request(root, 2000, hop_id="h3.t2"))
    await verify(tool_request(root, 2000, hop_id="h3.t2"))  # redelivery
    await store.append([EventDraft(trace_id=root.trace_id, type="WARNING", occurred_at=T0,
                                   payload={"note": "unrelated"})])  # fmt: skip
    m = await metrics(store)
    assert m["verdicts"] == 2
    assert m["decisions"] == {"ALLOW": 1, "BLOCK": 1}
    assert m["redeliveries"] == 1
    assert m["latency_ms"]["T1a"]["n"] == 2
    assert (await metrics(InMemoryStore()))["degraded_rate"] == 0.0
