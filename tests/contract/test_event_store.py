"""EventStore contract (§6.3, §12.1). Every adapter must pass; SQLite joins in Phase 3."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from constraintlens.adapters.store.memory import InMemoryStore
from constraintlens.domain.ports import EventStore
from tests.factories import make_draft

if TYPE_CHECKING:
    from collections.abc import Callable

FACTORIES: list[Callable[[], EventStore]] = [InMemoryStore]


@pytest.fixture(params=FACTORIES, ids=lambda f: f.__name__)
def store(request: pytest.FixtureRequest) -> EventStore:
    return request.param()


def test_satisfies_protocol(store: EventStore) -> None:
    assert isinstance(store, EventStore)


async def test_empty_store(store: EventStore) -> None:
    assert await store.read_after() == []
    assert await store.read_trace(uuid4()) == []


async def test_ids_start_at_one_and_increase_across_batches(store: EventStore) -> None:
    t = uuid4()
    a = await store.append([make_draft(t), make_draft(t)])
    b = await store.append([make_draft(t)])
    assert [e.event_id for e in a + b] == [1, 2, 3]


async def test_append_empty_batch_is_noop(store: EventStore) -> None:
    assert await store.append([]) == []
    assert await store.read_after() == []


async def test_append_preserves_content(store: EventStore) -> None:
    t = uuid4()
    draft = make_draft(t, type_="TRACE_OPENED", hop_id="h1")
    (event,) = await store.append([draft])
    assert event.model_dump(exclude={"event_id"}) == draft.model_dump()


async def test_read_trace_filters_and_orders(store: EventStore) -> None:
    t1, t2 = uuid4(), uuid4()
    await store.append([make_draft(t1), make_draft(t2), make_draft(t1)])
    events = await store.read_trace(t1)
    assert [e.event_id for e in events] == [1, 3]
    assert all(e.trace_id == t1 for e in events)


async def test_read_after_offset_trace_and_limit(store: EventStore) -> None:
    t1, t2 = uuid4(), uuid4()
    await store.append([make_draft(t1), make_draft(t2), make_draft(t1), make_draft(t1)])
    assert [e.event_id for e in await store.read_after(2)] == [3, 4]
    assert [e.event_id for e in await store.read_after(0, trace_id=t1)] == [1, 3, 4]
    assert [e.event_id for e in await store.read_after(1, trace_id=t1, limit=1)] == [3]
    assert await store.read_after(4) == []
    assert await store.read_after(0, limit=0) == []


async def test_stored_events_are_immutable(store: EventStore) -> None:
    (event,) = await store.append([make_draft(uuid4())])
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic frozen error type varies
        event.type = "WARNING"  # type: ignore[misc]
    (again,) = await store.read_after(0)
    assert again == event
