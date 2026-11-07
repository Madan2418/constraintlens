"""``InMemoryStore``: EventStore fake (§6.3). Same contract as the SQLite adapter."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from constraintlens.domain.models import Event, EventDraft


class InMemoryStore:
    def __init__(self) -> None:
        self._events: list[Event] = []
        self._lock = asyncio.Lock()  # single writer, mirrors the SQLite writer queue (§12.3)

    async def append(self, drafts: Sequence[EventDraft]) -> list[Event]:
        async with self._lock:
            start = len(self._events) + 1
            new = [d.with_id(start + i) for i, d in enumerate(drafts)]
            self._events.extend(new)
            return new

    async def read_trace(self, trace_id: UUID) -> list[Event]:
        return [e for e in self._events if e.trace_id == trace_id]

    async def read_after(
        self,
        after_event_id: int = 0,
        *,
        trace_id: UUID | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        out = [
            e
            for e in self._events[max(after_event_id, 0) :]
            if trace_id is None or e.trace_id == trace_id
        ]
        return out if limit is None else out[:limit]
