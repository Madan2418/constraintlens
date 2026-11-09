"""SQLite EventStore via SQLAlchemy (ARCHITECTURE.md §12.2, §12.3).

* ``events`` is the append-only source of truth.
* ``traces``, ``constraints`` and ``verdicts`` are projections, updated in the same
  transaction as the append and rebuildable with ``project_all()``.
  ``verdicts`` carries ``UNIQUE (trace_id, hop_id, checkpoint)``, so idempotency is
  also enforced by the database (§12.2).
* WAL mode, and one writer at a time through an asyncio lock (the "single writer
  queue" of §12.3).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    event,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from constraintlens.domain.errors import DependencyUnavailable
from constraintlens.domain.models import Event, JudgeResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncConnection

    from constraintlens.domain.models import EventDraft

metadata = MetaData()

events = Table(
    "events",
    metadata,
    Column("event_id", Integer, primary_key=True, autoincrement=True),
    Column("trace_id", String(36), nullable=False),
    Column("hop_id", String, nullable=True),
    Column("type", String, nullable=False),
    Column("occurred_at", String, nullable=False),  # ISO-8601 UTC
    Column("payload", Text, nullable=False),  # JSON
    Index("ix_events_trace", "trace_id", "event_id"),
    Index("ix_events_type_time", "type", "occurred_at"),
    sqlite_autoincrement=True,
)
traces = Table(
    "traces",
    metadata,
    Column("trace_id", String(36), primary_key=True),
    Column("opened_at", String),
    Column("closed_at", String),
    Column("status", String, nullable=False),
    Column("constraint_ids", Text),  # JSON array
    Column("outcome", String),
)
constraints = Table(
    "constraints",
    metadata,
    Column("constraint_id", String(36), primary_key=True),
    Column("trace_id", String(36), nullable=False),
    Column("scope", String, nullable=False),
    Column("owner", String, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("body", Text, nullable=False),
)
verdicts = Table(
    "verdicts",
    metadata,
    Column("verdict_id", String(36), primary_key=True),
    Column("trace_id", String(36), nullable=False),
    Column("hop_id", String, nullable=False),
    Column("checkpoint", String, nullable=False),
    Column("decision", String, nullable=False),
    Column("tier_used", String, nullable=False),
    Column("degraded", Boolean, nullable=False),
    UniqueConstraint("trace_id", "hop_id", "checkpoint", name="uq_verdict_idempotency"),
)
PROJECTIONS = (traces, constraints, verdicts)

#: Tier 2 verdict cache (§7.4, §12.2). Not a projection: never rebuilt from events.
judge_cache = Table(
    "judge_cache",
    metadata,
    Column("cache_key", String(64), primary_key=True),
    Column("verdict", String, nullable=False),
    Column("reason", Text, nullable=False),
    Column("confidence", Text, nullable=False),
    Column("evidence", Text, nullable=True),
    Column("model", String, nullable=False),
    Column("prompt_version", String, nullable=False),
    Column("created_at", String, nullable=False),
)


def async_url(url: str) -> str:
    """``sqlite:///x.db`` (§18 style) → ``sqlite+aiosqlite:///x.db``."""
    if url.startswith("sqlite+aiosqlite://"):
        return url
    if url.startswith("sqlite://"):
        return "sqlite+aiosqlite://" + url[len("sqlite://") :]
    raise ValueError(f"not a SQLite URL: {url!r}")


class SQLiteEventStore:
    def __init__(self, url: str = "sqlite:///:memory:") -> None:
        self.url = async_url(url)
        in_memory = ":memory:" in self.url
        if not in_memory:
            path = self.url.split(":///", 1)[1]
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._engine: AsyncEngine = create_async_engine(
            self.url, poolclass=StaticPool if in_memory else None
        )
        event.listen(self._engine.sync_engine, "connect", _sqlite_pragmas)
        self._write_lock = asyncio.Lock()
        self._ready = False

    async def initialize(self) -> SQLiteEventStore:
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        self._ready = True
        return self

    async def close(self) -> None:
        await self._engine.dispose()

    def judge_cache(self) -> SQLiteJudgeCache:
        """The Tier 2 cache in the same database file (§7.4 "backed by the EventStore")."""
        return SQLiteJudgeCache(self)

    # ------------------------------------------------------------------ writes

    async def append(self, drafts: Sequence[EventDraft]) -> list[Event]:
        await self._ensure_ready()
        if not drafts:
            return []
        async with self._write_lock, self._engine.begin() as conn:
            out: list[Event] = []
            for d in drafts:
                row = {
                    "trace_id": str(d.trace_id),
                    "hop_id": d.hop_id,
                    "type": d.type,
                    "occurred_at": d.occurred_at.isoformat(),
                    "payload": json.dumps(d.payload, sort_keys=True, default=str),
                }
                result = await conn.execute(
                    insert(events).values(**row).returning(events.c.event_id)
                )
                stored = d.with_id(int(result.scalar_one()))
                await _project(conn, stored)
                out.append(stored)
            return out

    async def project_all(self) -> None:
        """Rebuild every projection from the event log (§12.2)."""
        await self._ensure_ready()
        async with self._write_lock, self._engine.begin() as conn:
            for table in PROJECTIONS:
                await conn.execute(delete(table))
            rows = (await conn.execute(select(events).order_by(events.c.event_id))).mappings()
            for row in rows.all():
                await _project(conn, _to_event(row))

    # ------------------------------------------------------------------ reads

    async def read_trace(self, trace_id: UUID) -> list[Event]:
        return await self._select(events.c.trace_id == str(trace_id))

    async def read_after(
        self,
        after_event_id: int = 0,
        *,
        trace_id: UUID | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        cond = events.c.event_id > after_event_id
        if trace_id is not None:
            cond = cond & (events.c.trace_id == str(trace_id))
        return await self._select(cond, limit=limit)

    async def projection_rows(self) -> dict[str, list[dict[str, Any]]]:
        """Snapshot of all projection tables (used to prove they derive from the log)."""
        await self._ensure_ready()
        async with self._engine.connect() as conn:
            out = {}
            for table in PROJECTIONS:
                key = next(iter(table.primary_key.columns))
                rows = (await conn.execute(select(table).order_by(key))).mappings().all()
                out[table.name] = [dict(r) for r in rows]
            return out

    async def _select(self, cond: Any, *, limit: int | None = None) -> list[Event]:
        await self._ensure_ready()
        stmt = select(events).where(cond).order_by(events.c.event_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        try:
            async with self._engine.connect() as conn:
                rows = (await conn.execute(stmt)).mappings().all()
        except Exception as exc:  # pragma: no cover - surfaced as a port-level failure
            raise DependencyUnavailable("event store", str(exc)) from exc
        return [_to_event(r) for r in rows]

    async def _ensure_ready(self) -> None:
        if not self._ready:
            await self.initialize()


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _to_event(row: Any) -> Event:
    return Event(
        event_id=row["event_id"],
        trace_id=UUID(row["trace_id"]),
        type=row["type"],
        hop_id=row["hop_id"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        payload=json.loads(row["payload"]),
    )


async def _project(conn: AsyncConnection, e: Event) -> None:
    """Apply one event to the projection tables. Must stay a pure function of the event."""
    p = e.payload
    tid = str(e.trace_id)
    at = e.occurred_at.isoformat()
    if e.type == "TRACE_OPENED":
        await conn.execute(
            insert(traces).values(
                trace_id=tid,
                opened_at=at,
                status="open",
                constraint_ids=json.dumps(p.get("constraint_ids", [])),
                outcome="OPEN",
            )
        )
    elif e.type == "CONSTRAINT_REGISTERED":
        c = p["constraint"]
        await conn.execute(
            insert(constraints).values(
                constraint_id=c["constraint_id"],
                trace_id=tid,
                scope=c["scope"],
                owner=c["owner"],
                content_hash=c["content_hash"],
                body=json.dumps(c, sort_keys=True),
            )
        )
    elif e.type == "VERDICT_ISSUED":
        v = p["verdict"]
        await conn.execute(
            insert(verdicts).values(
                verdict_id=v["verdict_id"],
                trace_id=tid,
                hop_id=v["hop_id"],
                checkpoint=v["checkpoint"],
                decision=v["decision"],
                tier_used=v["tier_used"],
                degraded=bool(v["degraded"]),
            )
        )
    elif e.type == "TRACE_CLOSED":
        await conn.execute(
            update(traces)
            .where(traces.c.trace_id == tid)
            .values(closed_at=at, status="closed", outcome=p.get("outcome"))
        )
    elif e.type == "WARNING" and p.get("refused"):
        await conn.execute(
            insert(traces).values(trace_id=tid, opened_at=at, status="refused", outcome="REFUSED")
        )


class SQLiteJudgeCache:
    """JudgeCache on the ``judge_cache`` table, sharing the event store's engine."""

    def __init__(self, store: SQLiteEventStore) -> None:
        self._store = store

    async def get(self, key: str) -> JudgeResult | None:
        await self._store._ensure_ready()
        async with self._store._engine.connect() as conn:
            row = (
                await conn.execute(select(judge_cache).where(judge_cache.c.cache_key == key))
            ).first()
        if row is None:
            return None
        return JudgeResult(
            verdict=row.verdict,
            reason=row.reason,
            confidence=float(row.confidence),
            evidence=row.evidence,
        )

    async def put(self, key: str, result: JudgeResult, *, model: str, prompt_version: str) -> None:
        await self._store._ensure_ready()
        stmt = (
            sqlite_insert(judge_cache)
            .values(
                cache_key=key,
                verdict=result.verdict,
                reason=result.reason,
                confidence=repr(result.confidence),
                evidence=result.evidence,
                model=model,
                prompt_version=prompt_version,
                created_at=datetime.now(UTC).isoformat(),
            )
            .on_conflict_do_nothing(index_elements=["cache_key"])  # first answer wins
        )
        async with self._store._write_lock, self._store._engine.begin() as conn:
            await conn.execute(stmt)
