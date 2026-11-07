"""Property-based invariants for the Phase 0 foundations (§20).

Later phases add the async invariants (idempotent verdicts under any redelivery
order, reordering never overwrites later state, replay reproduces projections).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

from hypothesis import given, settings
from hypothesis import strategies as st

from constraintlens.adapters.clock import FrozenClock
from constraintlens.adapters.store.memory import InMemoryStore
from constraintlens.adapters.transport.memory import InMemoryQueue
from constraintlens.domain.models import Envelope, Limit
from tests.factories import make_constraint, make_draft, make_envelope

if TYPE_CHECKING:
    from decimal import Decimal

text = st.text(min_size=0, max_size=200)
amount = st.decimals(min_value=0, max_value=10**9, places=2, allow_nan=False, allow_infinity=False)


@given(
    payload=text,
    max_value=amount,
    hop_seq=st.integers(min_value=0, max_value=10_000),
    args=st.none() | st.dictionaries(st.text(min_size=1, max_size=10), st.integers()),
)
def test_envelope_json_round_trip_is_lossless(
    payload: str, max_value: Decimal, hop_seq: int, args: dict[str, int] | None
) -> None:
    c = make_constraint(limit=Limit(max_value=max_value, unit="USD"))
    env = make_envelope(text=payload, hop_seq=hop_seq, constraints=[c], args=args)
    assert Envelope.model_validate_json(env.model_dump_json()) == env


@given(batches=st.lists(st.integers(min_value=0, max_value=5), max_size=20))
def test_event_ids_are_gapless_and_increasing_for_any_batching(batches: list[int]) -> None:
    async def run() -> list[int]:
        store = InMemoryStore()
        t = uuid4()
        for n in batches:
            await store.append([make_draft(t) for _ in range(n)])
        return [e.event_id for e in await store.read_after(0)]

    ids = asyncio.run(run())
    assert ids == list(range(1, sum(batches) + 1))


@settings(max_examples=50)
@given(
    n_messages=st.integers(min_value=1, max_value=8),
    ops=st.lists(st.sampled_from(["consume", "reclaim", "ack"]), max_size=40),
)
def test_queue_never_loses_an_unacked_message(n_messages: int, ops: list[str]) -> None:
    """At-least-once: whatever happens, every message is either acked or still recoverable."""

    async def run() -> None:
        q = InMemoryQueue(FrozenClock())
        await q.ensure_group("s", "g")
        published = {await q.publish("s", make_envelope(hop_id=f"h{i}")) for i in range(n_messages)}
        acked: set[str] = set()
        held: list[str] = []
        for op in ops:
            if op == "consume":
                held += [d.message_id for d in await q.consume("s", "g", "w")]
            elif op == "reclaim":
                held += [d.message_id for d in await q.reclaim("s", "g", "w", min_idle_ms=0)]
            elif held:
                mid = held.pop(0)
                await q.ack("s", "g", mid)
                acked.add(mid)
        # Drain: consume new messages, then reclaim everything pending.
        recovered = {d.message_id for d in await q.consume("s", "g", "w", count=n_messages)}
        recovered |= {
            d.message_id for d in await q.reclaim("s", "g", "w", min_idle_ms=0, count=n_messages)
        }
        assert acked | recovered == published
        assert not (acked & recovered)

    asyncio.run(run())
