"""Failure-injection suite for the async boundary (ARCHITECTURE.md §10, §20, §23 Phase 4).

Each test injects one fault into a guarded two-stage Redis-worker pipeline
(``triage -> payments -> issue_refund``) and checks the failure-policy outcome.
Judge timeouts and malformed judge output are Phase 5 (no judge tier yet).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import fakeredis
import httpx
import pytest

from constraintlens.adapters.clock import FrozenClock
from constraintlens.adapters.store.memory import InMemoryStore
from constraintlens.adapters.transport.redis_streams import RedisStreamsTransport
from constraintlens.domain.errors import DependencyUnavailable
from constraintlens.domain.models import ActionBinding, Limit
from constraintlens.sdk import CircuitBreaker, ConstraintLens, GuardedWorker, next_envelope
from tests.factories import T0
from tests.service_helpers import WORKED, build_app

if TYPE_CHECKING:
    from fastapi import FastAPI

    from constraintlens.domain.models import Envelope
    from constraintlens.sdk import WorkerTask

BINDING = ActionBinding(action="refund", resource="order", amount_arg="amount", unit="USD")
GOOD = "Refund order #4471, limit $100, approval above."


class Switchable(httpx.AsyncBaseTransport):
    """An HTTP transport to the in-process service that can be cut."""

    def __init__(self, app: FastAPI) -> None:
        self.inner = httpx.ASGITransport(app=app)
        self.down = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        return await self.inner.handle_async_request(request)


class Rig:
    """Service + SDK + Redis Streams (fakeredis) + two guarded workers."""

    def __init__(self, app: FastAPI, container: Any, clock: FrozenClock) -> None:
        self.container, self.clock = container, clock
        self.http = Switchable(app)
        self.cl = ConstraintLens(
            "http://cl.test", transport=self.http, breaker=CircuitBreaker(threshold=100)
        )
        self.server = fakeredis.FakeServer()
        self.redis = fakeredis.FakeAsyncRedis(server=self.server, decode_responses=True)
        self.q = RedisStreamsTransport(self.redis, clock=clock)
        common: dict[str, Any] = {"group": "g", "clock": clock, "reclaim_idle_ms": 0}
        self.triage = GuardedWorker(
            self.cl,
            self.q,
            stream="triage",
            consumer="t1",
            agent_id="triage",
            out_stream="payments",
            **common,
        )
        self.payments = GuardedWorker(self.cl, self.q, stream="payments", consumer="p1",
                                      agent_id="payments", **common)  # fmt: skip
        self.refunds: list[int] = []

        @self.cl.guard_tool(name="issue_refund", binding=BINDING)
        async def issue_refund(order: int, amount: int) -> None:
            self.refunds.append(amount)

        self.issue_refund = issue_refund

    @classmethod
    async def build(cls, **overrides: Any) -> Rig:
        clock = FrozenClock(T0)
        app, container = await build_app(store=InMemoryStore(), clock=clock, **overrides)
        rig = cls(app, container, clock)
        await rig.triage.start()
        await rig.payments.start()
        return rig

    async def open(self) -> Envelope:
        root = (await self.cl.open_trace(WORKED, owner="u")).envelope
        await self.q.publish("triage", root)
        return root

    async def triage_handler(self, task: WorkerTask) -> str:
        return GOOD

    async def pay_handler(self, task: WorkerTask) -> None:
        await self.issue_refund(order=4471, amount=100)

    async def pending(self, stream: str) -> int:
        info: Any = await self.redis.xpending(stream, "g")
        return int(info["pending"])


async def test_service_unreachable_blocks_without_losing_the_message() -> None:
    rig = await Rig.build()
    await rig.open()
    await rig.triage.poll(rig.triage_handler)
    rig.http.down = True  # the service dies mid-trace
    (r,) = await rig.payments.poll(rig.pay_handler)
    assert (r.outcome, r.acked) == ("unavailable", False)  # fail closed (high risk)...
    assert r.verdict is not None
    assert r.verdict.degraded
    assert r.verdict.decision == "BLOCK"
    assert rig.refunds == []
    assert await rig.pending("payments") == 1

    rig.http.down = False
    (r2,) = await rig.payments.poll(rig.pay_handler)  # reclaimed once the service is back
    assert (r2.outcome, r2.acked) == ("done", True)
    assert rig.refunds == [100]


async def test_low_risk_fails_open_when_the_service_is_unreachable() -> None:
    rig = await Rig.build()
    root = (await rig.cl.open_trace(WORKED, owner="u", risk_class="low")).envelope
    await rig.q.publish("payments", root)
    rig.http.down = True
    # on_unavailable["low"] == "open": CONSUME and the tool gate both answer locally with a
    # degraded ALLOW_WITH_WARNING, so the task proceeds and the message is acknowledged.
    (r,) = await rig.payments.poll(rig.pay_handler)
    assert (r.outcome, r.acked) == ("done", True)
    assert rig.refunds == [100]


async def test_event_store_outage_blocks_and_recovers() -> None:
    rig = await Rig.build()
    await rig.open()
    store = rig.container.store
    real_read = store.read_trace

    async def broken(*_: Any, **__: Any) -> Any:
        raise DependencyUnavailable("event store", "disk unavailable")

    store.read_trace = broken
    (r,) = await rig.triage.poll(rig.triage_handler)
    assert (r.outcome, r.acked) == ("unavailable", False)  # 503 -> SDK fails closed
    store.read_trace = real_read
    (r2,) = await rig.triage.poll(rig.triage_handler)
    assert (r2.outcome, r2.acked) == ("done", True)


async def test_redis_outage_mid_trace_is_reported_not_masked() -> None:
    rig = await Rig.build()
    await rig.open()
    await rig.triage.poll(rig.triage_handler)
    rig.server.connected = False  # Redis restarts
    with pytest.raises(DependencyUnavailable, match="redis"):
        await rig.payments.poll(rig.pay_handler)
    rig.server.connected = True  # back, with its data: nothing was lost
    (r,) = await rig.payments.poll(rig.pay_handler)
    assert r.outcome == "done"
    assert rig.refunds == [100]


@pytest.mark.parametrize("fault", ["tampered", "stripped"])
async def test_envelope_altered_in_transit_is_blocked(fault: str) -> None:
    rig = await Rig.build()
    root = await rig.open()
    e1 = next_envelope(root, hop_id="h1", agent_id="triage", text=GOOD, emitted_at=T0)
    if fault == "tampered":  # someone raised the limit in the queue
        c = e1.constraints[0]
        loose = c.model_copy(update={"limit": Limit(max_value=Decimal(5000), unit="USD")})
        e1 = e1.model_copy(update={"constraints": [loose]})
    else:
        e1 = e1.model_copy(update={"constraints": []})
    await rig.triage.poll(rig.triage_handler)  # the genuine hop h1 checks in
    await rig.q.publish("payments", e1)
    results = await rig.payments.poll(rig.pay_handler, count=2)
    altered = [r for r in results if r.verdict and r.verdict.failures]
    (r,) = altered
    assert (r.outcome, r.acked) == ("blocked", True)
    expected = "HASH_MISMATCH" if fault == "tampered" else "CONSTRAINT_LOST"
    assert expected in [f.kind for f in r.verdict.failures]


async def test_message_lost_in_transit_is_flagged_by_the_sweeper() -> None:
    rig = await Rig.build(consume_timeout_s=600)
    root = await rig.open()
    real_publish = rig.q.publish

    async def drop(stream: str, envelope: Any) -> str:
        return "0-0" if stream == "payments" else await real_publish(stream, envelope)

    rig.q.publish = drop  # type: ignore[method-assign]
    (r,) = await rig.triage.poll(rig.triage_handler)
    assert r.outcome == "done"  # triage thinks it handed off
    assert await rig.payments.poll(rig.pay_handler) == []  # nothing ever arrives
    rig.clock.advance(601)
    (flag,) = await rig.container.sweep()
    assert flag.payload["published_hop_id"] == "h1"
    assert flag.payload["destination"] == "payments"
    trace = await rig.cl.get_trace(root.trace_id)
    assert len(trace["transit_losses"]) == 1


async def test_reordered_delivery_is_flagged_and_not_conflicting() -> None:
    rig = await Rig.build()
    root = await rig.open()
    await rig.triage.poll(rig.triage_handler)  # h1 consumed root, published e1
    (first,) = await rig.payments.poll(rig.pay_handler)  # h2 consumed e1
    assert first.outcome == "done"
    await rig.q.publish("triage", root)  # a stale copy of the root task arrives late
    rig.triage.consumer = "t2"
    (late,) = await rig.triage.poll(rig.triage_handler)
    # Same hop id h1: the stored verdicts answer it; its tool-less re-run re-publishes,
    # and downstream answers from the stored verdicts too. No conflicting decision.
    assert late.outcome == "done"
    (again,) = await rig.payments.poll(rig.pay_handler)
    assert again.outcome == "duplicate"
    assert rig.refunds == [100]


async def test_concurrent_duplicate_deliveries_execute_once() -> None:
    rig = await Rig.build()
    await rig.open()
    await rig.triage.poll(rig.triage_handler)
    (d,) = await rig.q.consume("payments", "g", "p1")
    dup = d.model_copy(update={"delivery_count": 2})  # reclaimed while still in flight
    a, b = await asyncio.gather(
        rig.payments.process(d, rig.pay_handler), rig.payments.process(dup, rig.pay_handler)
    )
    assert sorted([a.outcome, b.outcome]) == ["done", "duplicate"]
    assert rig.refunds == [100]
