"""QueueTransport contract (§6.3, §9.1, §9.6): in-memory fake and Redis Streams."""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import fakeredis
import pytest

from constraintlens.adapters.transport.memory import InMemoryQueue
from constraintlens.adapters.transport.redis_streams import RedisStreamsTransport
from constraintlens.domain.ports import QueueTransport
from tests.factories import make_envelope

if TYPE_CHECKING:
    from collections.abc import Callable


def _memory() -> QueueTransport:
    return InMemoryQueue()


def _redis_fake() -> QueueTransport:
    return RedisStreamsTransport(fakeredis.FakeAsyncRedis(decode_responses=True))


FACTORIES: list[Callable[[], QueueTransport]] = [_memory, _redis_fake]
if os.environ.get("REDIS_URL"):  # real Redis joins when available (make integration)

    def _redis_real() -> QueueTransport:
        from redis.asyncio import Redis

        return RedisStreamsTransport(Redis.from_url(os.environ["REDIS_URL"], decode_responses=True))

    FACTORIES.append(_redis_real)

S, G = f"tasks-{uuid.uuid4().hex[:8]}", "workers"


@pytest.fixture(params=FACTORIES, ids=lambda f: f.__name__.lstrip("_"))
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


async def test_unknown_group_is_a_lookup_error(queue: QueueTransport) -> None:
    with pytest.raises(LookupError, match="does not exist"):
        await queue.consume(S, "no-such-group", "c")


async def test_enqueued_at_is_set_by_the_transport(queue: QueueTransport) -> None:
    await queue.publish(S, make_envelope())
    (d,) = await queue.consume(S, G, "c")
    assert d.enqueued_at.tzinfo is not None
