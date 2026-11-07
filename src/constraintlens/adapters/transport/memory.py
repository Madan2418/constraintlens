"""``InMemoryQueue``: QueueTransport fake with Redis-Streams semantics (§6.3, §9.1).

Models consumer groups, a pending-entries list, at-least-once re-delivery through
``reclaim``, and per-message delivery counts, so async edge cases can be tested
without Redis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from constraintlens.adapters.clock import SystemClock
from constraintlens.domain.models import Delivery, Envelope

if TYPE_CHECKING:
    from datetime import datetime

    from constraintlens.domain.ports import Clock


@dataclass
class _Message:
    message_id: str
    envelope_json: str
    enqueued_at: datetime


@dataclass
class _Pending:
    consumer: str
    delivery_count: int
    last_delivered_mono: float


@dataclass
class _Group:
    next_index: int = 0
    pending: dict[str, _Pending] = field(default_factory=dict)


class InMemoryQueue:
    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._streams: dict[str, list[_Message]] = {}
        self._groups: dict[tuple[str, str], _Group] = {}
        self._seq = 0

    async def ensure_group(self, stream: str, group: str) -> None:
        self._streams.setdefault(stream, [])
        self._groups.setdefault((stream, group), _Group())

    async def publish(self, stream: str, envelope: Envelope) -> str:
        self._seq += 1
        message_id = f"{self._seq}-0"
        # Serialise on publish so consumers get a copy, as with a real broker.
        self._streams.setdefault(stream, []).append(
            _Message(message_id, envelope.model_dump_json(), self._clock.now())
        )
        return message_id

    async def consume(
        self, stream: str, group: str, consumer: str, *, count: int = 1
    ) -> list[Delivery]:
        g = self._group(stream, group)
        messages = self._streams[stream]
        batch = messages[g.next_index : g.next_index + count]
        g.next_index += len(batch)
        now = self._clock.monotonic()
        for m in batch:
            g.pending[m.message_id] = _Pending(consumer, 1, now)
        return [self._delivery(stream, m, 1) for m in batch]

    async def reclaim(
        self, stream: str, group: str, consumer: str, *, min_idle_ms: int, count: int = 1
    ) -> list[Delivery]:
        g = self._group(stream, group)
        now = self._clock.monotonic()
        out: list[Delivery] = []
        for m in self._streams[stream]:
            if len(out) >= count:
                break
            p = g.pending.get(m.message_id)
            if p is None or (now - p.last_delivered_mono) * 1000 < min_idle_ms:
                continue
            p.consumer = consumer
            p.delivery_count += 1
            p.last_delivered_mono = now
            out.append(self._delivery(stream, m, p.delivery_count))
        return out

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        self._group(stream, group).pending.pop(message_id, None)

    def pending_count(self, stream: str, group: str) -> int:
        """Test helper: number of delivered-but-unacked messages."""
        return len(self._group(stream, group).pending)

    def _group(self, stream: str, group: str) -> _Group:
        try:
            return self._groups[(stream, group)]
        except KeyError:
            raise LookupError(f"consumer group {group!r} on {stream!r} does not exist") from None

    @staticmethod
    def _delivery(stream: str, m: _Message, delivery_count: int) -> Delivery:
        return Delivery(
            stream=stream,
            message_id=m.message_id,
            envelope=Envelope.model_validate_json(m.envelope_json),
            delivery_count=delivery_count,
            enqueued_at=m.enqueued_at,
        )
