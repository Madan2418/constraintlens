from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID

import pytest

from constraintlens.adapters.clock import FrozenClock
from constraintlens.adapters.judge.scripted import ScriptedJudge
from constraintlens.adapters.store.memory import InMemoryStore
from constraintlens.application.open_trace import OpenTrace, OpenTraceRequest
from constraintlens.domain.canonical import compute_envelope_digest, verify_content_hash
from constraintlens.domain.errors import UnsatisfiableConstraints
from constraintlens.domain.models import ConstraintSpec, Limit
from tests.factories import T0

WORKED = "Refund this customer, up to $100. Anything above needs manager approval."
ORG_50 = ConstraintSpec(
    scope="org", owner="org:acme", origin="policy", action="refund", resource="order",
    limit=Limit(max_value=Decimal(50), unit="USD"),
)  # fmt: skip


def ids() -> object:
    n = iter(range(1, 1000))
    return lambda: UUID(int=next(n))


def use_case(store: InMemoryStore, **kw: object) -> OpenTrace:
    return OpenTrace(store=store, clock=FrozenClock(T0), id_factory=ids(), **kw)  # type: ignore[arg-type]


async def test_opens_trace_with_extracted_and_declared_constraints() -> None:
    store = InMemoryStore()
    result = await use_case(store)(OpenTraceRequest(WORKED, owner="user:alice", declared=[ORG_50]))

    assert result.trace_id == UUID(int=1)
    user, org = result.constraints
    assert (user.scope, org.scope) == ("user", "org")
    assert org.max_age_seconds == 3600  # default freshness applied to declared policy
    assert all(verify_content_hash(c) for c in result.constraints)
    assert all(c.issued_at == T0 for c in result.constraints)

    env = result.envelope
    assert (env.hop.hop_id, env.hop.hop_seq, env.payload_text) == ("root", 0, WORKED)
    assert env.envelope_digest == compute_envelope_digest(result.trace_id, result.constraints)
    assert [c.kind for c in result.composition.conflicts] == ["SUPERSEDED"]

    events = await store.read_trace(result.trace_id)
    assert [e.type for e in events] == [
        "TRACE_OPENED",
        "CONSTRAINT_REGISTERED",
        "CONSTRAINT_REGISTERED",
    ]
    opened = events[0].payload
    assert "instruction" not in opened  # store_full_text is off by default (§12.4)
    assert opened["instruction_chars"] == len(WORKED)
    assert opened["constraint_ids"] == [str(user.constraint_id), str(org.constraint_id)]
    assert opened["conflicts"][0]["kind"] == "SUPERSEDED"
    json.dumps(opened)  # payload is JSON-serialisable
    registered = events[1].payload["constraint"]
    assert registered["content_hash"] == user.content_hash


async def test_full_text_opt_in() -> None:
    store = InMemoryStore()
    result = await use_case(store, store_full_text=True)(OpenTraceRequest(WORKED, owner="u"))
    (opened, *_) = await store.read_trace(result.trace_id)
    assert opened.payload["instruction"] == WORKED


async def test_unsatisfiable_is_refused_and_recorded() -> None:
    store = InMemoryStore()
    other_order = ORG_50.model_copy(update={"subject": "order:9"})
    with pytest.raises(UnsatisfiableConstraints, match="no common item"):
        await use_case(store)(
            OpenTraceRequest("Refund order #4471 up to $100.", owner="u", declared=[other_order])
        )
    (event,) = await store.read_after(0)
    assert event.type == "WARNING"
    assert event.payload["refused"] is True


async def test_llm_fallback_used_through_use_case() -> None:
    text = "Refund the client, keeping it under a hundred dollars."
    llm = json.dumps({"constraints": [{
        "action": "refund", "resource": "order", "subject": "*",
        "limit": {"max_value": "100", "unit": "USD"}, "authority": "none", "expiry": None,
    }]})  # fmt: skip
    judge = ScriptedJudge(extractions={text: llm})
    store = InMemoryStore()
    result = await use_case(store, judge=judge)(OpenTraceRequest(text, owner="u"))
    assert result.extraction.sources == ["llm"]
    assert len(result.constraints) == 1
    (opened, _) = await store.read_trace(result.trace_id)
    assert opened.payload["extraction"]["llm_called"] is True
