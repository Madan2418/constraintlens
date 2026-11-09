"""``SweepTransitLoss``: loss in transit, case (d) (ARCHITECTURE.md §7.5, §9.5).

A ``PUBLISH`` checkpoint that names a destination queue (``transport_meta.destination``)
announces an envelope about to enter that queue. If no ``CONSUME`` of that envelope
appears within ``consume_timeout_s``, the message was lost or is stuck. The sweeper
appends one ``WARNING`` per such hop, with ``transit_loss=true`` and a ``MISSING_HOP``
failure, blamed on the queue edge rather than an agent (§7.7 step 4, ``TRANSIT_LOSS``).

It is a background reader of the event log, not part of any request. It reads
incrementally from a cursor. Its state is rebuilt from offset 0 on start, including the
losses it already flagged, so a restart never flags a hop twice. A publish whose
verdict blocked (or asked to re-ground) never left the agent and is not expected
downstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from constraintlens.domain.models import BLOCKING_DECISIONS, EventDraft

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from constraintlens.domain.models import Event
    from constraintlens.domain.ports import Clock, EventStore


@dataclass(frozen=True)
class _Published:
    hop_id: str
    destination: str
    at: datetime


class SweepTransitLoss:
    def __init__(
        self, *, store: EventStore, clock: Clock, consume_timeout_s: float, batch: int = 1000
    ) -> None:
        if consume_timeout_s <= 0:
            raise ValueError("consume_timeout_s must be positive")
        self._store = store
        self._clock = clock
        self._timeout = consume_timeout_s
        self._batch = batch
        self._cursor = 0
        self._pending: dict[tuple[UUID, str], _Published] = {}
        self._flagged: set[tuple[UUID, str]] = set()
        self._last_publish: dict[UUID, tuple[UUID, str]] = {}  # trace -> publish awaiting verdict

    async def __call__(self) -> list[Event]:
        """Catch up with the log, then flag every publish overdue for consumption."""
        while True:
            events = await self._store.read_after(self._cursor, limit=self._batch)
            for e in events:
                self._apply(e)
                self._cursor = e.event_id
            if len(events) < self._batch:
                break
        now = self._clock.now()
        overdue = [
            (key, pub)
            for key, pub in self._pending.items()
            if key not in self._flagged and (now - pub.at).total_seconds() >= self._timeout
        ]
        if not overdue:
            return []
        drafts = [self._draft(trace_id, pub, now) for (trace_id, _), pub in overdue]
        written = await self._store.append(drafts)
        for e in written:
            self._apply(e)
            self._cursor = max(self._cursor, e.event_id)
        return written

    def _apply(self, e: Event) -> None:
        p: dict[str, Any] = e.payload
        if e.type == "CHECKPOINT_RECEIVED":
            req = p["request"]
            hop_id = req["envelope"]["hop"]["hop_id"]
            key = (e.trace_id, hop_id)
            if req["checkpoint"] == "CONSUME":
                self._pending.pop(key, None)  # consumed (late or not): nothing to flag
            elif req["checkpoint"] == "PUBLISH":
                destination = (req.get("transport_meta") or {}).get("destination")
                if destination and key not in self._pending:
                    self._pending[key] = _Published(hop_id, destination, e.occurred_at)
                    self._last_publish[e.trace_id] = key
        elif e.type == "VERDICT_ISSUED" and p["verdict"]["checkpoint"] == "PUBLISH":
            if e.trace_id in self._last_publish:  # same batch as its checkpoint
                key = self._last_publish.pop(e.trace_id)
                if p["verdict"]["decision"] in BLOCKING_DECISIONS:
                    self._pending.pop(key, None)  # never left the agent
        elif e.type == "WARNING" and p.get("transit_loss"):
            self._flagged.add((e.trace_id, p["published_hop_id"]))

    def _draft(self, trace_id: UUID, pub: _Published, now: datetime) -> EventDraft:
        waited = (now - pub.at).total_seconds()
        return EventDraft(
            trace_id=trace_id,
            type="WARNING",
            hop_id=pub.hop_id,
            occurred_at=now,
            payload={
                "transit_loss": True,
                "published_hop_id": pub.hop_id,
                "destination": pub.destination,
                "published_at": pub.at.isoformat(),
                "consume_timeout_s": self._timeout,
                "failure": {
                    "kind": "MISSING_HOP",
                    "constraint_id": None,
                    "detail": f"envelope of {pub.hop_id} published to {pub.destination!r} "
                    f"was not consumed within {self._timeout:.0f}s (waited {waited:.0f}s)",
                },
            },
        )
