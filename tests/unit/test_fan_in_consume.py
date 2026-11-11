"""Fan-in joins consume one message per parent (§9.4, §9.6, ADR 0010).

Found by the Phase 8 evaluation: with the key (trace, hop, CONSUME), a join hop's
CONSUME of its second parent collided with the first, was treated as a conflicting
duplicate, and every redelivery of it got a fresh verdict. Each consumed message is
now its own checkpoint, in both stores.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from constraintlens.adapters.clock import FrozenClock
from constraintlens.adapters.store.memory import InMemoryStore
from constraintlens.adapters.store.sqlite import SQLiteEventStore
from constraintlens.application.open_trace import OpenTrace, OpenTraceRequest
from constraintlens.application.verify_checkpoint import VerifyCheckpoint
from constraintlens.domain.checker.pipeline import TieredChecker
from constraintlens.domain.checker.tier0_integrity import IntegrityTier
from constraintlens.domain.enforcement import Policy
from constraintlens.domain.models import CheckpointRequest, Envelope, HopMeta, TransportMeta
from constraintlens.domain.projections import fold
from tests.factories import T0
from tests.service_helpers import WORKED

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from constraintlens.domain.ports import EventStore


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[EventStore]:
    if request.param == "memory":
        yield InMemoryStore()
        return
    s = await SQLiteEventStore("sqlite:///:memory:").initialize()
    yield s
    await s.close()


def child(parent: Envelope, hop: str, *, parents: list[Envelope] | None = None) -> Envelope:
    ps = parents or [parent]
    return Envelope(
        trace_id=parent.trace_id,
        hop=HopMeta(
            hop_id=hop, parent_hop_ids=[p.hop.hop_id for p in ps],
            hop_seq=max(p.hop.hop_seq for p in ps) + 1, agent_id=f"agent-{hop}", emitted_at=T0,
        ),
        constraints=list(parent.constraints), envelope_digest=parent.envelope_digest,
        payload_text="Refund up to $100; approval above.",
    )  # fmt: skip


async def test_join_consumes_each_parent_once_and_redelivery_is_idempotent(
    store: EventStore,
) -> None:
    clock = FrozenClock(T0)
    root = (await OpenTrace(store=store, clock=clock)(OpenTraceRequest(WORKED, owner="u"))).envelope
    verify = VerifyCheckpoint(
        store=store, clock=clock, checker=TieredChecker([IntegrityTier()], clock), policy=Policy()
    )

    async def consume(hop: str, env: Envelope, delivery: int = 1) -> Any:
        return await verify.run(
            CheckpointRequest(
                trace_id=root.trace_id, hop_id=hop, checkpoint="CONSUME", envelope=env,
                transport_meta=TransportMeta(delivery_count=delivery),
            )
        )  # fmt: skip

    await consume("h1", root)
    h1 = child(root, "h1")
    await consume("h2", h1)
    await consume("h3", h1)
    h2, h3 = child(h1, "h2"), child(h1, "h3")

    first_a = await consume("h4", h2)
    first_b = await consume("h4", h3)  # the second parent: its own checkpoint
    assert not first_a.replayed
    assert not first_b.replayed
    assert first_a.verdict.verdict_id != first_b.verdict.verdict_id

    again_a = await consume("h4", h2, delivery=2)
    again_b = await consume("h4", h3, delivery=2)
    assert again_a.replayed
    assert again_b.replayed
    assert again_a.verdict.verdict_id == first_a.verdict.verdict_id
    assert again_b.verdict.verdict_id == first_b.verdict.verdict_id

    state = fold(root.trace_id, await store.read_trace(root.trace_id))
    assert state.conflicts == []  # neither join message is a conflicting duplicate
    assert {"h2", "h3"} <= state.history.parents["h4"]
    joins = [v for v in state.verdicts if v.hop_id == "h4"]
    assert len(joins) == 2
