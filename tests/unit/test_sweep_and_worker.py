"""Transit-loss sweeper (§9.5 d), the Redis-worker adapter (§7.3), and SDK redelivery (§9.6)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import fakeredis
import pytest

from constraintlens.adapters.clock import FrozenClock
from constraintlens.adapters.store.memory import InMemoryStore
from constraintlens.adapters.transport.memory import InMemoryQueue
from constraintlens.adapters.transport.redis_streams import RedisStreamsTransport
from constraintlens.application.sweep_transit import SweepTransitLoss
from constraintlens.application.trace_queries import load_trace, summarise
from constraintlens.domain.models import ActionBinding
from constraintlens.sdk import (
    CheckpointHeld,
    ConstraintViolation,
    DuplicateToolCall,
    GuardedWorker,
    next_envelope,
)
from tests.factories import T0, make_envelope
from tests.service_helpers import WORKED, ScriptedSemanticTier, build_app, sdk_client

if TYPE_CHECKING:
    from constraintlens.sdk import ConstraintLens, WorkerTask

BINDING = ActionBinding(action="refund", resource="order", amount_arg="amount", unit="USD")


# --------------------------------------------------------------------------- sweeper


async def _published_trace(clock: FrozenClock, **overrides: Any) -> tuple[Any, Any, Any]:
    store = InMemoryStore()
    app, container = await build_app(store=store, clock=clock, **overrides)
    cl = sdk_client(app)
    opened = await cl.open_trace(WORKED, owner="u")
    await cl.consume(opened.envelope, hop_id="h1")
    e1 = next_envelope(opened.envelope, hop_id="h1", agent_id="intake",
                       text="Refund #4471, limit $100, approval above.", emitted_at=T0)  # fmt: skip
    assert (await cl.publish(e1, destination="tasks:triage")).decision == "ALLOW"
    return container, cl, (opened, e1)


async def test_sweeper_flags_a_publish_never_consumed_once() -> None:
    clock = FrozenClock(T0)
    container, cl, (opened, _) = await _published_trace(clock, consume_timeout_s=600)
    sweep = container.sweep
    assert await sweep() == []  # within the timeout
    clock.advance(601)
    (flag,) = await sweep()
    assert flag.type == "WARNING"
    assert flag.payload["transit_loss"]
    assert flag.payload["failure"]["kind"] == "MISSING_HOP"
    assert "published to 'tasks:triage'" in flag.payload["failure"]["detail"]
    assert await sweep() == []  # never flagged twice

    # A restarted sweeper rebuilds from the log and does not re-flag either.
    fresh = SweepTransitLoss(store=container.store, clock=clock, consume_timeout_s=600)
    assert await fresh() == []
    summary = summarise(await load_trace(container.store, opened.trace_id))
    assert [t["published_hop_id"] for t in summary["transit_losses"]] == ["h1"]
    await cl.aclose()


async def test_consumed_or_blocked_publishes_are_not_losses() -> None:
    clock = FrozenClock(T0)
    container, cl, (_, e1) = await _published_trace(
        clock, consume_timeout_s=60, semantic_tier=ScriptedSemanticTier()
    )
    await cl.consume(e1, hop_id="h2")  # consumed: not lost
    drifted = next_envelope(e1, hop_id="h2", agent_id="triage", text="Refund in full.",
                            emitted_at=T0)  # fmt: skip
    v = await cl.publish(drifted, destination="tasks:payments")
    assert v.decision == "RE_GROUND"  # never left the agent
    no_destination = next_envelope(e1, hop_id="h2b", agent_id="triage", text="$100 ok",
                                   emitted_at=T0)  # fmt: skip
    await cl.publish(no_destination)  # direct handoff: nothing to sweep
    clock.advance(3600)
    assert await container.sweep() == []
    await cl.aclose()


def test_sweeper_rejects_a_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="positive"):
        SweepTransitLoss(store=InMemoryStore(), clock=FrozenClock(T0), consume_timeout_s=0)


# --------------------------------------------------------------------------- GuardedWorker


class Pipeline:
    """Two guarded workers on an in-memory queue against an in-process service."""

    def __init__(self, cl: ConstraintLens, clock: FrozenClock) -> None:
        self.cl, self.clock = cl, clock
        self.q = InMemoryQueue(clock)
        self.executed: list[dict[str, Any]] = []
        common: dict[str, Any] = {"group": "g", "clock": clock, "reclaim_idle_ms": 0}
        self.triage = GuardedWorker(
            cl,
            self.q,
            stream="triage",
            consumer="t1",
            agent_id="triage",
            out_stream="payments",
            **common,
        )
        self.payments = GuardedWorker(cl, self.q, stream="payments", consumer="p1",
                                      agent_id="payments", **common)  # fmt: skip

        @cl.guard_tool(name="issue_refund", binding=BINDING)
        async def issue_refund(order: int, amount: int) -> None:
            self.executed.append({"order": order, "amount": amount})

        self.issue_refund = issue_refund

    async def start(self, text: str = WORKED) -> Any:
        await self.triage.start()
        await self.payments.start()
        opened = await self.cl.open_trace(WORKED, owner="u")
        root = opened.envelope.model_copy(update={"payload_text": text})
        # The service saw no checkpoint for h0 yet: the first consumer observes it.
        await self.q.publish("triage", root)
        return opened


async def test_worker_runs_consume_tool_and_publish_and_acks() -> None:
    clock = FrozenClock(T0)
    app, _ = await build_app(clock=clock)
    async with sdk_client(app) as cl:
        p = Pipeline(cl, clock)
        opened = await p.start()

        async def triage(task: WorkerTask) -> str:
            assert task.envelope.hop.hop_id == "h1"
            assert not task.redelivered
            return "Refund order #4471, limit $100, approval above."

        async def payments(task: WorkerTask) -> None:
            await p.issue_refund(order=4471, amount=100)

        (r1,) = await p.triage.poll(triage)
        assert (r1.outcome, r1.acked, r1.published_id) == ("done", True, "2-0")
        (r2,) = await p.payments.poll(payments)
        assert (r2.outcome, r2.hop_id) == ("done", "h2")
        assert p.executed == [{"order": 4471, "amount": 100}]
        assert p.q.pending_count("triage", "g") == p.q.pending_count("payments", "g") == 0

        trace = await cl.get_trace(opened.trace_id)
        assert [(v["hop_id"], v["checkpoint"]) for v in trace["verdicts"]] == [
            ("h1", "CONSUME"), ("h1", "PUBLISH"), ("h2", "CONSUME"), ("h2.t1", "TOOL_CALL"),
        ]  # fmt: skip


async def test_redelivered_tool_call_is_not_executed_twice() -> None:
    clock = FrozenClock(T0)
    app, _ = await build_app(clock=clock)
    async with sdk_client(app) as cl:
        p = Pipeline(cl, clock)
        await p.start()
        crashed = {"once": True}

        async def triage(task: WorkerTask) -> str:
            return "Refund order #4471, limit $100, approval above."

        async def payments(task: WorkerTask) -> None:
            await p.issue_refund(order=4471, amount=100)
            if crashed["once"]:
                crashed["once"] = False
                raise RuntimeError("worker died before ack")

        await p.triage.poll(triage)
        with pytest.raises(RuntimeError):
            await p.payments.poll(payments)
        assert p.q.pending_count("payments", "g") == 1  # not acked: will be redelivered
        p.payments.consumer = "p2"
        (r,) = await p.payments.poll(payments)
        assert (r.outcome, r.acked) == ("duplicate", True)
        assert r.verdict is not None
        assert r.verdict.decision == "ALLOW"
        assert p.executed == [{"order": 4471, "amount": 100}]  # once, not twice
        metrics = (await cl._http.get("/v1/metrics")).json()
        assert metrics["redeliveries"] == 2  # the CONSUME and the TOOL_CALL


async def test_opt_in_reexecution_of_redelivered_tools() -> None:
    clock = FrozenClock(T0)
    app, _ = await build_app(clock=clock)
    async with sdk_client(app, reexecute_redelivered_tools=True) as cl:
        env = (await cl.open_trace(WORKED, owner="u")).envelope

        @cl.guard_tool(name="issue_refund", binding=BINDING)
        async def issue_refund(order: int, amount: int) -> str:
            return "done"

        for _ in range(2):
            async with cl.hop(env):
                assert await issue_refund(order=4471, amount=50) == "done"


async def test_duplicate_tool_call_signal_without_a_worker() -> None:
    app, _ = await build_app()
    async with sdk_client(app) as cl:
        env = (await cl.open_trace(WORKED, owner="u")).envelope

        @cl.guard_tool(name="issue_refund", binding=BINDING)
        async def issue_refund(order: int, amount: int) -> None: ...

        async with cl.hop(env):
            await issue_refund(order=4471, amount=50)
        with pytest.raises(DuplicateToolCall, match="already authorized"):
            async with cl.hop(env):  # the same hop re-run: same sub-hop id h0.t1
                await issue_refund(order=4471, amount=50)


async def test_held_consume_is_left_pending_and_retried() -> None:
    clock = FrozenClock(T0)
    app, _ = await build_app(clock=clock, join_timeout_s=60)
    async with sdk_client(app) as cl:
        p = Pipeline(cl, clock)
        opened = await p.start()
        e1 = next_envelope(opened.envelope, hop_id="h1", agent_id="intake",
                           text="Refund #4471, limit $100.", emitted_at=T0)  # fmt: skip
        e2 = next_envelope(e1, hop_id="h2", agent_id="triage",
                           text="Refund #4471, limit $100.", emitted_at=T0)  # fmt: skip
        await p.q.publish("payments", e2)  # h1 never checked in: a gap at payments

        async def payments(task: WorkerTask) -> None:
            raise AssertionError("must not run while held")

        (r,) = await p.payments.poll(payments)
        assert (r.outcome, r.acked) == ("held", False)
        assert p.q.pending_count("payments", "g") == 1
        clock.advance(61)

        async def payments_ok(task: WorkerTask) -> None:
            return None

        (r2,) = await p.payments.poll(payments_ok)  # reclaimed, join timed out
        assert (r2.outcome, r2.acked) == ("done", True)


async def test_blocked_consume_and_publish_are_acked() -> None:
    clock = FrozenClock(T0)
    app, _ = await build_app(clock=clock, semantic_tier=ScriptedSemanticTier())
    async with sdk_client(app, max_regrounds=1) as cl:
        p = Pipeline(cl, clock)
        await p.start()
        attempts: list[str] = []

        async def drifting(task: WorkerTask) -> str:
            attempts.append(task.text)
            return "Refund order #4471 in full."  # keeps dropping the limit

        (r,) = await p.triage.poll(drifting)
        assert (r.outcome, r.acked) == ("blocked", True)
        assert r.hop_id == "h1/r1"  # re-grounded once, then blocked (max_regrounds)
        assert "Constraint reminder:" in attempts[1]

        stale_env = next_envelope((await cl.open_trace(WORKED, owner="u")).envelope,
                                  hop_id="h1", agent_id="x", text="no", emitted_at=T0)  # fmt: skip
        await p.q.publish("payments", stale_env.model_copy(update={"constraints": []}))
        (lost,) = await p.payments.poll(drifting)
        assert (lost.outcome, lost.acked) == ("blocked", True)
        assert lost.verdict is not None
        assert lost.verdict.failures[0].kind == "CONSTRAINT_LOST"


async def test_tool_block_inside_a_worker_is_acked() -> None:
    clock = FrozenClock(T0)
    app, _ = await build_app(clock=clock)
    async with sdk_client(app) as cl:
        p = Pipeline(cl, clock)
        await p.start()

        async def payments_greedy(task: WorkerTask) -> None:
            await p.issue_refund(order=4471, amount=2000)

        await p.q.publish("payments", (await p.triage.transport.consume("triage", "g", "x"))[0]
                          .envelope)  # fmt: skip
        (r,) = await p.payments.poll(payments_greedy)
        assert (r.outcome, r.acked) == ("blocked", True)
        assert p.executed == []


async def test_guard_hop_raises_checkpoint_held() -> None:
    from constraintlens.adapters.pipeline.langgraph import LangGraphStateAdapter

    app, _ = await build_app()
    async with sdk_client(app) as cl:
        root = (await cl.open_trace(WORKED, owner="u")).envelope
        orphan = next_envelope(next_envelope(root, hop_id="h1", agent_id="a", text="t"),
                               hop_id="h2", agent_id="b", text="t")  # fmt: skip

        @cl.guard_hop(agent_id="c", adapter=LangGraphStateAdapter())
        async def agent(state: dict[str, Any]) -> dict[str, Any]:
            return state

        with pytest.raises(CheckpointHeld) as exc:
            await agent({"text": "t", "cl_envelope": orphan.model_dump(mode="json")})
        assert isinstance(exc.value, ConstraintViolation)
        assert exc.value.verdict.held


# --------------------------------------------------------------------------- transport


async def test_redis_transport_can_stamp_ids_from_a_virtual_clock() -> None:
    clock = FrozenClock(T0)
    q = RedisStreamsTransport(fakeredis.FakeAsyncRedis(decode_responses=True), clock=clock)
    await q.ensure_group("s", "g")
    await q.publish("s", make_envelope())
    clock.advance(45 * 60)
    await q.publish("s", make_envelope())
    first, second = await q.consume("s", "g", "c", count=2)
    assert first.enqueued_at == T0
    assert (second.enqueued_at - first.enqueued_at).total_seconds() == 45 * 60

    behind = RedisStreamsTransport(q._r, clock=FrozenClock(T0.replace(year=2020)))
    with pytest.raises(ValueError, match="clock moved backwards"):
        await behind.publish("s", make_envelope())
