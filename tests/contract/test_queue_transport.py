"""QueueTransport contract (§6.3, §9.1, §9.6). Redis Streams joins in Phase 4."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from constraintlens.adapters.transport.memory import InMemoryQueue
from constraintlens.domain.ports import QueueTransport
from tests.factories import make_envelope

if TYPE_CHECKING:
    from collections.abc import Callable

FACTORIES: list[Callable[[], QueueTransport]] = [InMemoryQueue]
S, G = "tasks", "workers"


@pytest.fixture(params=FACTORIES, ids=lambda f: f.__name__)
async def queue(request: pytest.FixtureRequest) -> QueueTransport:
    q = request.param()
    await q.ensure_group(S, G)
    return q


def test_satisfies_protocol(queue: QueueTransport) -> None:
    assert isinstance(queue, QueueTransport)


async def test_ensure_group_is_idempotent(queue: QueueTransport) -> None:
    await queue.ensure_group(S, G)
    assert await queue.consume(S, G, "c") == []


async def test_round_trip_preserves_envelope(queue: QueueTransport) -> None:
    env = make_envelope(text="Refund order #4471, limit $100.", args={"amount": 50})
    message_id = await queue.publish(S, env)
    (d,) = await queue.consume(S, G, "c")
    assert d.message_id == message_id
    assert d.stream == S
    assert d.envelope == env
    assert d.delivery_count == 1


async def test_fifo_and_each_message_delivered_once_per_group(queue: QueueTransport) -> None:
    ids = [await queue.publish(S, make_envelope(hop_id=f"h{i}")) for i in range(3)]
    first = await queue.consume(S, G, "a", count=2)
    second = await queue.consume(S, G, "b", count=5)
    assert [d.message_id for d in first + second] == ids
    assert await queue.consume(S, G, "a") == []


async def test_groups_are_independent(queue: QueueTransport) -> None:
    await queue.ensure_group(S, "auditors")
    await queue.publish(S, make_envelope())
    assert len(await queue.consume(S, G, "a")) == 1
    assert len(await queue.consume(S, "auditors", "x")) == 1


async def test_unacked_message_is_redelivered_with_higher_count(queue: QueueTransport) -> None:
    await queue.publish(S, make_envelope())
    (d1,) = await queue.consume(S, G, "a")
    (d2,) = await queue.reclaim(S, G, "b", min_idle_ms=0)
    assert d2.message_id == d1.message_id
    assert d2.envelope == d1.envelope
    assert d2.delivery_count == d1.delivery_count + 1


async def test_acked_message_is_not_redelivered(queue: QueueTransport) -> None:
    await queue.publish(S, make_envelope())
    (d,) = await queue.consume(S, G, "a")
    await queue.ack(S, G, d.message_id)
    assert await queue.reclaim(S, G, "b", min_idle_ms=0) == []


async def test_ack_is_idempotent(queue: QueueTransport) -> None:
    await queue.publish(S, make_envelope())
    (d,) = await queue.consume(S, G, "a")
    await queue.ack(S, G, d.message_id)
    await queue.ack(S, G, d.message_id)
