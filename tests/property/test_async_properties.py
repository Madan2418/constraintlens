"""Property tests for the async boundary (ARCHITECTURE.md §20, §23 Phase 4 exit criteria).

* Verdicts are idempotent under any redelivery order, including concurrent duplicates:
  a checkpoint never gets two different final verdicts, and the log holds one final
  verdict per key.
* Reordering never lets a later hop's state be overwritten: the consumed frontier only
  grows, and a consume is flagged OUT_OF_ORDER exactly when a descendant was consumed
  first.
* Replaying the log reproduces every decision, holds included.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from constraintlens.adapters.clock import FrozenClock
from constraintlens.adapters.store.memory import InMemoryStore
from constraintlens.application.open_trace import OpenTrace, OpenTraceRequest
from constraintlens.application.trace_queries import ReplayTrace
from constraintlens.application.verify_checkpoint import VerifyCheckpoint
from constraintlens.domain.async_guard import GuardConfig
from constraintlens.domain.checker.pipeline import TieredChecker
from constraintlens.domain.checker.tier0_integrity import IntegrityTier
from constraintlens.domain.checker.tier1a_action import ActionTier
from constraintlens.domain.enforcement import Policy
from constraintlens.domain.models import (
    ActionBinding,
    CheckpointRequest,
    Envelope,
    ProposedAction,
    TransportMeta,
)
from constraintlens.domain.projections import default_idempotency_key, fold
from constraintlens.sdk import next_envelope
from tests.factories import T0
from tests.service_helpers import WORKED

BINDING = ActionBinding(action="refund", resource="order", amount_arg="amount", unit="USD")
GUARD = GuardConfig(dwell_threshold_s=300, join_timeout_s=60)
SETTINGS = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])


async def _service() -> tuple[InMemoryStore, VerifyCheckpoint, TieredChecker, Envelope]:
    store, clock = InMemoryStore(), FrozenClock(T0)
    opened = await OpenTrace(store=store, clock=clock)(OpenTraceRequest(WORKED, owner="u"))
    checker = TieredChecker([IntegrityTier(), ActionTier()], clock)
    verify = VerifyCheckpoint(
        store=store, clock=clock, checker=checker, policy=Policy(), guard=GUARD
    )
    return store, verify, checker, opened.envelope


def _chain(root: Envelope, n: int) -> list[Envelope]:
    """root (h0) -> h1 -> ... -> hn, each emitted by the hop that consumed its parent."""
    out = [root]
    for i in range(1, n + 1):
        out.append(next_envelope(out[-1], hop_id=f"h{i}", agent_id=f"a{i}",
                                 text="Refund #4471, limit $100.", emitted_at=T0))  # fmt: skip
    return out


def _requests(chain: list[Envelope], amount: int) -> list[CheckpointRequest]:
    """Every checkpoint of a linear run: CONSUME/PUBLISH per hop, a tool gate at the end."""
    reqs: list[CheckpointRequest] = []
    tid = chain[0].trace_id
    for i in range(1, len(chain)):
        reqs.append(CheckpointRequest(trace_id=tid, hop_id=f"h{i}", checkpoint="CONSUME",
                                      envelope=chain[i - 1]))  # fmt: skip
        reqs.append(CheckpointRequest(trace_id=tid, hop_id=f"h{i}", checkpoint="PUBLISH",
                                      envelope=chain[i]))  # fmt: skip
    reqs.append(
        CheckpointRequest(
            trace_id=tid, hop_id=f"h{len(chain) - 1}.t1", checkpoint="TOOL_CALL",
            envelope=chain[-1],
            proposed_action=ProposedAction(tool="issue_refund", args={"amount": amount},
                                           binding=BINDING),
        )
    )  # fmt: skip
    return reqs


@SETTINGS
@given(
    n=st.integers(min_value=1, max_value=3),
    amount=st.sampled_from([50, 100, 2000]),
    data=st.data(),
)
def test_verdicts_are_idempotent_under_any_redelivery_order(
    n: int, amount: int, data: st.DataObject
) -> None:
    async def run() -> None:
        store, verify, checker, root = await _service()
        reqs = _requests(_chain(root, n), amount)
        # Every checkpoint is delivered at least once, some several times, in any order;
        # some deliveries arrive concurrently (same batch).
        deliveries = list(range(len(reqs))) + data.draw(
            st.lists(st.integers(0, len(reqs) - 1), max_size=8), label="redeliveries"
        )
        order = data.draw(st.permutations(deliveries), label="order")
        batches = data.draw(
            st.lists(st.integers(1, 3), min_size=len(order), max_size=len(order)), label="batch"
        )
        responses: dict[str, list[Any]] = defaultdict(list)
        i = 0
        while i < len(order):
            batch = order[i : i + batches[i]]
            i += len(batch)
            results = await asyncio.gather(
                *(verify(reqs[j].model_copy(update={"transport_meta": TransportMeta(
                    delivery_count=1 + order[:i].count(j))})) for j in batch)
            )  # fmt: skip
            for v in results:
                responses[default_idempotency_key(v.trace_id, v.hop_id, v.checkpoint)].append(v)

        # Redeliver anything still held, now that every predecessor has been seen.
        for r in reqs:
            key = default_idempotency_key(r.trace_id, r.hop_id, r.checkpoint)
            if all(v.held for v in responses[key]):
                responses[key].append(await verify(r))

        events = await store.read_trace(root.trace_id)
        final_per_key: dict[str, int] = defaultdict(int)
        for e in events:
            if e.type == "VERDICT_ISSUED":
                final_per_key[e.payload["idempotency_key"]] += 1
        for key, vs in responses.items():
            finals = {v.verdict_id: v for v in vs if not v.held}
            assert len(finals) == 1, f"{key}: conflicting final verdicts {finals}"
            assert final_per_key[key] == 1
        assert set(final_per_key) == set(responses)

        # Replay reproduces every decision, held ones included.
        items = await ReplayTrace(store=store, guard=GUARD)(
            root.trace_id, checker=checker, policy=Policy()
        )
        assert not [i for i in items if i.changed]

    asyncio.run(run())


@SETTINGS
@given(n=st.integers(min_value=2, max_value=5), data=st.data())
def test_reordering_never_overwrites_later_state(n: int, data: st.DataObject) -> None:
    async def run() -> None:
        store, verify, _, root = await _service()
        chain = _chain(root, n)
        tid = root.trace_id
        for i in range(1, n + 1):  # every hop has announced itself: no gaps, only reordering
            await verify(CheckpointRequest(trace_id=tid, hop_id=f"h{i}", checkpoint="PUBLISH",
                                           envelope=chain[i]))  # fmt: skip
        order = data.draw(st.permutations(range(n)), label="consume order")
        consumed_before: set[int] = set()
        frontier: set[str] = set()
        for k in order:  # consumer h{k+1} consumes envelope chain[k]
            consume = CheckpointRequest(
                trace_id=tid, hop_id=f"h{k + 1}", checkpoint="CONSUME", envelope=chain[k]
            )
            v = await verify(consume)
            late = any(j > k for j in consumed_before)
            assert ("OUT_OF_ORDER" in [f.kind for f in v.failures]) == late
            assert not v.held
            assert v.decision == ("ALLOW_WITH_WARNING" if late else "ALLOW")
            consumed_before.add(k)
            state = fold(tid, await store.read_trace(tid))
            assert frontier <= state.history.consumed  # never moves backwards
            frontier = set(state.history.consumed)
        assert frontier == {e.hop.hop_id for e in chain[:n]}

    asyncio.run(run())
