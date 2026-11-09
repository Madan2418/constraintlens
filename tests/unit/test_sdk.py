"""SDK: guards, re-grounding, circuit breaker and fail policy (§7.3, §10)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest

from constraintlens.adapters.pipeline.langgraph import LangGraphStateAdapter
from constraintlens.domain.models import ActionBinding
from constraintlens.sdk import (
    CircuitBreaker,
    ConstraintLens,
    ConstraintLensClientError,
    ConstraintViolation,
    ServiceUnavailable,
    current_hop,
)
from tests.service_helpers import WORKED, build_app, sdk_client

BINDING = ActionBinding(action="refund", resource="order", amount_arg="amount", unit="USD")
ADAPTER = LangGraphStateAdapter()


def failing_transport(status: int | None = None) -> httpx.MockTransport:
    def handler(_: httpx.Request) -> httpx.Response:
        if status is None:
            raise httpx.ConnectError("refused")
        return httpx.Response(status, json={"title": "boom"})

    return httpx.MockTransport(handler)


async def test_guarded_tool_blocks_and_does_not_run() -> None:
    app, _ = await build_app()
    executed: list[float] = []
    async with sdk_client(app) as cl:
        trace = await cl.open_trace(WORKED, owner="u")

        @cl.guard_tool(name="issue_refund", binding=BINDING)
        async def issue_refund(order: int, amount: float) -> str:
            executed.append(amount)
            return "ok"

        with pytest.raises(RuntimeError, match="outside a hop"):
            await issue_refund(1, 5)
        async with cl.hop(trace.envelope) as ctx:
            assert current_hop() is ctx
            assert await issue_refund(4471, amount=100) == "ok"
            with pytest.raises(ConstraintViolation) as exc:
                await issue_refund(4471, 2000)
            assert ctx.tool_calls == 2
        assert current_hop() is None
        assert executed == [100]
        assert exc.value.verdict.decision == "BLOCK"
        assert exc.value.verdict.hop_id == "root.t2"

        events = await cl.events(trace.trace_id)
        assert [e["type"] for e in events].count("VERDICT_ISSUED") == 2
        assert (await cl.get_trace(trace.trace_id))["status"] == "open"
        assert (await cl.replay(trace.trace_id))["changed"] == 0


async def test_kwargs_tools_record_named_arguments() -> None:
    app, _ = await build_app()
    async with sdk_client(app) as cl:
        trace = await cl.open_trace(WORKED, owner="u")

        @cl.guard_tool(name="issue_refund", binding=BINDING)
        async def run(**args: Any) -> None:
            return None

        async with cl.hop(trace.envelope):
            with pytest.raises(ConstraintViolation):
                await run(order=1, amount=500)
        events = await cl.events(trace.trace_id)
        received = next(e for e in events if e["type"] == "CHECKPOINT_RECEIVED")
        assert received["payload"]["request"]["proposed_action"]["args"] == {
            "order": 1,
            "amount": 500,
        }


async def test_guard_hop_consume_publish_and_reground() -> None:
    app, _ = await build_app(drift_tier=True)
    calls: list[str] = []
    async with sdk_client(app) as cl:
        trace = await cl.open_trace(WORKED, owner="u")

        @cl.guard_hop(agent_id="triage", adapter=ADAPTER)
        async def triage(state: dict[str, Any]) -> dict[str, Any]:
            calls.append(state["text"])
            # Drops the limit unless reminded of it (as a re-grounded agent would).
            keep = "$100" if "Constraint reminder" in state["text"] else ""
            return {**state, "text": f"Resolve with a refund {keep}".strip()}

        state = ADAPTER.write_envelope({"text": "Process refund, limit $100."}, trace.envelope)
        out = await triage(state)
        assert out["text"] == "Resolve with a refund $100"
        assert len(calls) == 2
        assert "100 USD" in calls[1]  # canonical constraint text re-injected
        emitted = ADAPTER.read_envelope(out)
        assert emitted is not None
        assert emitted.hop.hop_id == "h1/r1"
        assert emitted.hop.parent_hop_ids == ["root"]
        summary = await cl.get_trace(trace.trace_id)
        assert [(v["hop_id"], v["checkpoint"], v["decision"]) for v in summary["verdicts"]] == [
            ("h1", "CONSUME", "ALLOW"),
            ("h1", "PUBLISH", "RE_GROUND"),
            ("h1/r1", "PUBLISH", "ALLOW"),
        ]


async def test_guard_hop_blocks_on_lost_envelope_and_stripped_constraints() -> None:
    app, _ = await build_app()
    async with sdk_client(app) as cl:
        trace = await cl.open_trace(WORKED, owner="u")

        @cl.guard_hop(agent_id="triage", adapter=ADAPTER)
        async def triage(state: dict[str, Any]) -> dict[str, Any]:
            return state

        with pytest.raises(ConstraintViolation, match="envelope missing"):
            await triage({"text": "hi"})
        stripped = trace.envelope.model_copy(update={"constraints": []})
        with pytest.raises(ConstraintViolation) as exc:
            await triage(ADAPTER.write_envelope({"text": "hi"}, stripped))
        assert exc.value.verdict.failures[0].kind == "CONSTRAINT_LOST"


async def test_reground_limit_raises() -> None:
    app, _ = await build_app(drift_tier=True, max_regrounds=1)
    async with sdk_client(app, max_regrounds=1) as cl:
        trace = await cl.open_trace(WORKED, owner="u")

        @cl.guard_hop(agent_id="triage", adapter=ADAPTER)
        async def stubborn(state: dict[str, Any]) -> dict[str, Any]:
            return {**state, "text": "Resolve with a refund."}

        with pytest.raises(ConstraintViolation) as exc:
            await stubborn(ADAPTER.write_envelope({"text": "x"}, trace.envelope))
        assert "re-ground limit" in exc.value.verdict.reason


async def test_unavailable_service_fails_closed_for_high_risk() -> None:
    app, _ = await build_app()
    async with sdk_client(app) as good:
        trace = await good.open_trace(WORKED, owner="u")
    async with ConstraintLens("http://down", transport=failing_transport()) as cl:
        verdict = await cl.publish(trace.envelope)
        assert (verdict.decision, verdict.degraded) == ("BLOCK", True)
        assert "failing closed" in verdict.reason
        with pytest.raises(ServiceUnavailable):
            await cl.open_trace(WORKED, owner="u")
    low = trace.envelope.model_copy(
        update={
            "constraints": [
                c.model_copy(update={"risk_class": "low"}) for c in trace.envelope.constraints
            ]
        }
    )
    async with ConstraintLens("http://down", transport=failing_transport(503)) as cl:
        verdict = await cl.publish(low)
        assert verdict.decision == "ALLOW_WITH_WARNING"
    async with ConstraintLens("http://down", transport=failing_transport(503),
                              on_unavailable={"low": "closed"}) as cl:  # fmt: skip
        assert (await cl.publish(low)).decision == "BLOCK"


async def test_client_errors_raise() -> None:
    async with ConstraintLens("http://x", transport=failing_transport(404)) as cl:
        with pytest.raises(ConstraintLensClientError) as exc:
            await cl.get_trace(uuid4())
        assert exc.value.status == 404
    plain = httpx.MockTransport(lambda _r: httpx.Response(400, text="not json"))
    async with ConstraintLens("http://x", transport=plain) as cl:
        with pytest.raises(ConstraintLensClientError, match="400"):
            await cl.events(uuid4())


def test_circuit_breaker_states() -> None:
    now = [0.0]
    b = CircuitBreaker(threshold=2, reset_s=10, clock=lambda: now[0])
    assert b.state == "closed"
    b.failure()
    assert b.allow()
    b.failure()
    assert (b.state, b.allow()) == ("open", False)
    now[0] = 10.0
    assert b.state == "half_open"
    b.failure()  # trial failed: open again
    assert b.state == "open"
    now[0] = 25.0
    b.success()
    assert b.state == "closed"


async def test_open_breaker_skips_the_network() -> None:
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request)
        raise httpx.ConnectError("down")

    app, _ = await build_app()
    async with sdk_client(app) as good:
        trace = await good.open_trace(WORKED, owner="u")
    breaker = CircuitBreaker(threshold=1, reset_s=60)
    async with ConstraintLens("http://down", transport=httpx.MockTransport(handler),
                              breaker=breaker) as cl:  # fmt: skip
        await cl.publish(trace.envelope)
        verdict = await cl.publish(trace.envelope)
    assert len(hits) == 1
    assert "circuit breaker open" in verdict.reason
