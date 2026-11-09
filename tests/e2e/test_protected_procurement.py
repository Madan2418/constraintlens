"""Phase 4 end to end: the procurement pipeline on Redis Streams workers, protected.

Same scripted agents, drift and at-least-once failure as the baseline; only the SDK
differs. Runs on ``RedisStreamsTransport`` over fakeredis (real Redis with REDIS_URL).
"""

from __future__ import annotations

import os
from collections import Counter
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from constraintlens.adapters.store.sqlite import SQLiteEventStore
from pipelines import procurement_redis_workers as protected
from pipelines.baseline import procurement_redis_workers as baseline
from pipelines.common.agents import render_report
from pipelines.common.facts import MUTATIONS
from tests.service_helpers import ScriptedSemanticTier, build_app, sdk_client

if TYPE_CHECKING:
    from pipelines.common.facts import Mutation

SHORT = {1: 0.0, 2: 30.0, 3: 120.0}  # under the 5-minute dwell threshold
CASES = [
    (org, m, hop)
    for org in (False, True)
    for m in MUTATIONS
    if org or m != "CONSTRAINT_DROP_ONE"
    for hop in ((None,) if m == "NONE" else (1, 2, 3))
]


@pytest.mark.parametrize(("org", "mutation", "hop"), CASES)
async def test_every_baseline_violation_is_blocked_and_nothing_else(
    org: bool, mutation: Mutation, hop: int | None
) -> None:
    before = await baseline.run(mutation, hop, org_policy=org)
    app, _ = await build_app()
    async with sdk_client(app) as cl:
        after = await protected.run(cl, mutation, hop, org_policy=org, dwell_s=SHORT)
    assert after.violations == []
    if before.violations:
        assert after.blocked
        assert after.tool_calls == []
    else:  # no false blocks: the same tools run as without ConstraintLens
        assert after.blocked == []
        assert after.halted is None
        assert [c.tool for c in after.tool_calls] == [c.tool for c in before.tool_calls]


async def test_redelivery_executes_once_with_one_verdict_per_checkpoint() -> None:
    store = await SQLiteEventStore("sqlite:///:memory:").initialize()
    app, _ = await build_app(store=store)
    async with sdk_client(app) as cl:
        res = await protected.run(cl, crash_before_ack=True, dwell_s=SHORT)
        replay = await cl.replay(UUID(res.trace_id))
    baseline_res = await baseline.run(crash_before_ack=True)
    assert len(baseline_res.tool_calls) == 2  # the problem §9.6 exists for

    assert [c.tool for c in res.tool_calls] == ["place_order"]  # once, not twice
    assert res.violations == []
    assert [h.delivery for h in res.hops] == [1, 1, 1, 2]
    report = render_report(res)
    assert "redelivery #2" in report
    assert "not executed again" in report

    events = await store.read_trace(UUID(res.trace_id))
    finals = Counter(
        (e.payload["verdict"]["hop_id"], e.payload["verdict"]["checkpoint"])
        for e in events
        if e.type == "VERDICT_ISSUED"
    )
    assert set(finals.values()) == {1}  # no checkpoint was decided twice
    redeliveries = [e for e in events if e.type == "WARNING" and e.payload.get("redelivery")]
    assert sorted(e.hop_id for e in redeliveries) == ["h3", "h3.t1"]  # CONSUME, TOOL_CALL
    assert replay["changed"] == 0
    await store.close()


async def test_long_dwell_fails_closed_until_semantic_tiers_exist() -> None:
    """45 minutes in the queue: the budget must be re-verified (§9.3). No semantic tier."""
    app, _ = await build_app()
    async with sdk_client(app) as cl:
        res = await protected.run(cl)  # default dwell: 45 minutes before purchasing
    assert res.tool_calls == []
    assert res.violations == []
    assert res.halted is not None
    assert "queue dwell 2700s exceeds 300s" in res.halted
    assert "no semantic tier could re-verify" in res.halted
    assert [h.hop for h in res.hops] == [1, 2]  # purchasing never ran


async def test_long_dwell_with_reverification_proceeds() -> None:
    app, _ = await build_app(semantic_tier=ScriptedSemanticTier("$15,000"))
    async with sdk_client(app) as cl:
        res = await protected.run(cl)
        trace = await cl.get_trace(UUID(res.trace_id))
    assert [c.tool for c in res.tool_calls] == ["place_order"]
    consume_h3 = next(
        v for v in trace["verdicts"] if (v["hop_id"], v["checkpoint"]) == ("h3", "CONSUME")
    )
    assert consume_h3["decision"] == "ALLOW"
    assert not consume_h3["degraded"]


async def test_drift_is_regrounded_then_blocked_at_publish() -> None:
    app, _ = await build_app(semantic_tier=ScriptedSemanticTier("$15,000"))
    async with sdk_client(app) as cl:
        res = await protected.run(cl, "SUMMARIZATION_LOSS", 2)
        trace = await cl.get_trace(UUID(res.trace_id))
    assert res.tool_calls == []
    assert res.halted is not None
    sourcing = [(v["hop_id"], v["decision"]) for v in trace["verdicts"]
                if v["checkpoint"] == "PUBLISH" and v["hop_id"].startswith("h2")]  # fmt: skip
    assert sourcing == [("h2", "RE_GROUND"), ("h2/r1", "RE_GROUND"), ("h2/r2", "BLOCK")]


def test_cli(capsys: pytest.CaptureFixture[str]) -> None:
    protected.main(["--crash-before-ack", "--dwell-minutes", "2", "--compare"])
    out = capsys.readouterr().out
    assert "duplicate execution" in out  # baseline half
    assert "not executed again" in out  # protected half
    protected.main(["--mutation", "SUMMARIZATION_LOSS", "--hop", "2"])
    assert "Pipeline halted by ConstraintLens" in capsys.readouterr().out


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("REDIS_URL"), reason="needs a real Redis (REDIS_URL)")
async def test_protected_against_real_redis() -> None:
    app, _ = await build_app()
    async with sdk_client(app) as cl:
        res = await protected.run(
            cl, crash_before_ack=True, dwell_s=SHORT, redis_url=os.environ["REDIS_URL"]
        )
    assert [c.tool for c in res.tool_calls] == ["place_order"]
