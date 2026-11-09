"""Redis Streams QueueTransport (ARCHITECTURE.md §9.1).

Consumer groups give at-least-once delivery: ``consume`` reads new entries (XREADGROUP
``>``), ``reclaim`` takes over entries another consumer left pending (XAUTOCLAIM), and
``ack`` removes them from the pending list (XACK). A delivery's ``delivery_count`` is
Redis's own ``times_delivered`` counter, and ``enqueued_at`` comes from the stream id's
millisecond timestamp. Neither is supplied by the producer, so a producer cannot forge
dwell time or redelivery.

``clock`` exists for simulation only (demos and tests on a virtual clock): the stream id
then takes its millisecond part from that clock (``<ms>-*``), so a 45-minute queue dwell
can be reproduced in milliseconds. Production code leaves it unset and Redis assigns
the id from its own clock.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from redis.exceptions import RedisError, ResponseError

from constraintlens.domain.errors import DependencyUnavailable
from constraintlens.domain.models import Delivery, Envelope

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from constraintlens.domain.ports import Clock

ENVELOPE_FIELD = "envelope"


def enqueued_at(message_id: str) -> datetime:
    """Stream ids are ``<ms since epoch>-<seq>``."""
    millis = int(message_id.split("-", 1)[0])
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


class RedisStreamsTransport:
    def __init__(self, client: Redis, *, clock: Clock | None = None) -> None:
        """``client`` must be created with ``decode_responses=True``."""
        self._r = client
        self._clock = clock

    async def ensure_group(self, stream: str, group: str) -> None:
        try:
            await self._r.xgroup_create(stream, group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        except RedisError as exc:
            raise DependencyUnavailable("redis", str(exc)) from exc

    async def publish(self, stream: str, envelope: Envelope) -> str:
        fields: dict[Any, Any] = {ENVELOPE_FIELD: envelope.model_dump_json()}
        message_id = "*"
        if self._clock is not None:  # simulation only, see module docstring
            message_id = f"{int(self._clock.now().timestamp() * 1000)}-*"
        try:
            assigned = await self._r.xadd(stream, fields, id=message_id)
        except ResponseError as exc:
            raise ValueError(f"stream id rejected (clock moved backwards?): {exc}") from exc
        except RedisError as exc:
            raise DependencyUnavailable("redis", str(exc)) from exc
        return assigned if isinstance(assigned, str) else assigned.decode()

    async def consume(
        self, stream: str, group: str, consumer: str, *, count: int = 1
    ) -> list[Delivery]:
        try:
            reply: Any = await self._r.xreadgroup(group, consumer, {stream: ">"}, count=count)
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                raise LookupError(f"consumer group {group!r} on {stream!r} does not exist") from exc
            raise
        except RedisError as exc:
            raise DependencyUnavailable("redis", str(exc)) from exc
        out = []
        for _stream, messages in reply or []:
            for message_id, fields in messages:
                out.append(self._delivery(stream, message_id, fields, delivery_count=1))
        return out

    async def reclaim(
        self, stream: str, group: str, consumer: str, *, min_idle_ms: int, count: int = 1
    ) -> list[Delivery]:
        try:
            reply: Any = await self._r.xautoclaim(
                stream, group, consumer, min_idle_time=min_idle_ms, start_id="0-0", count=count
            )
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                raise LookupError(f"consumer group {group!r} on {stream!r} does not exist") from exc
            raise
        except RedisError as exc:
            raise DependencyUnavailable("redis", str(exc)) from exc
        out = []
        for message_id, fields in reply[1]:
            if not fields:  # entry deleted from the stream while pending
                continue
            pending: Any = await self._r.xpending_range(
                stream, group, min=message_id, max=message_id, count=1
            )
            times = int(pending[0]["times_delivered"]) if pending else 2
            out.append(self._delivery(stream, message_id, fields, delivery_count=times))
        return out

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        try:
            await self._r.xack(stream, group, message_id)
        except RedisError as exc:
            raise DependencyUnavailable("redis", str(exc)) from exc

    @staticmethod
    def _delivery(
        stream: str, message_id: str, fields: dict[str, str], *, delivery_count: int
    ) -> Delivery:
        return Delivery(
            stream=stream,
            message_id=message_id,
            envelope=Envelope.model_validate_json(fields[ENVELOPE_FIELD]),
            delivery_count=delivery_count,
            enqueued_at=enqueued_at(message_id),
        )
