"""Phase 3 exit criteria (ARCHITECTURE.md §23):

1. action violations are blocked in the LangGraph pipeline;
2. the event log is complete and replayable.

The same scripted agents and drift as the baseline; only ConstraintLens differs.
"""

from __future__ import annotations

import argparse
from collections import Counter
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from constraintlens.adapters.store.sqlite import SQLiteEventStore
from constraintlens.domain.models import Event
from constraintlens.domain.projections import fold
from pipelines import refund_langgraph as protected
from pipelines.baseline import refund_langgraph as baseline
from pipelines.common.agents import render_report
from tests.service_helpers import build_app, sdk_client

if TYPE_CHECKING:
    from pipelines.common.facts import Mutation

# What the protected pipeline does per mutation (hop 2), and why.
#   blocked  - Tier 1a stops the drifted tool call; nothing harmful executes
#   ok       - no harmful action was attempted
#   missed   - documented gap: "this customer" names no order, so the subject is "*" and
#              refunding another order is structurally in scope (needs Phase 5 tiers)
EXPECTED: dict[bool, dict[Mutation, str]] = {
    False: {"NONE": "ok", "NUMERIC_LOOSEN": "blocked", "QUALIFIER_DELETE": "ok",
            "NEGATION_FLIP": "blocked", "SCOPE_WIDEN": "missed", "SUMMARIZATION_LOSS": "blocked",
            "INJECTED_INSTRUCTION": "blocked"},
    True: {"NONE": "ok", "NUMERIC_LOOSEN": "ok", "QUALIFIER_DELETE": "ok", "NEGATION_FLIP": "ok",
           "SCOPE_WIDEN": "missed", "SUMMARIZATION_LOSS": "blocked",
           "INJECTED_INSTRUCTION": "blocked", "CONSTRAINT_DROP_ONE": "blocked"},
}  # fmt: skip
CASES = [(org, m, hop, out) for org, t in EXPECTED.items() for m, out in t.items()
         for hop in ((1,) if m == "NONE" else (1, 2, 3))]  # fmt: skip


@pytest.mark.parametrize(("org", "mutation", "hop", "expected"), CASES)
async def test_blocks_action_violations(
    org: bool, mutation: Mutation, hop: int, expected: str
) -> None:
    app, _ = await build_app()
    before = await baseline.run(mutation, None if mutation == "NONE" else hop, org_policy=org)
    async with sdk_client(app) as cl:
        after = await protected.run(
            cl, mutation, None if mutation == "NONE" else hop, org_policy=org
        )
    assert after.halted is None
    if expected == "blocked":
        assert before.silent_violation  # the baseline lets it through...
        assert after.violations == []  # ...ConstraintLens does not
        assert after.blocked
    elif expected == "ok":
        assert after.violations == []
        assert after.blocked == []
        assert [c.tool for c in after.tool_calls] == [c.tool for c in before.tool_calls]
    else:
        assert after.violations == before.violations != []


async def test_worked_example_blocked_and_fully_recorded() -> None:
    store = await SQLiteEventStore("sqlite:///:memory:").initialize()
    app, _ = await build_app(store=store)
    async with sdk_client(app) as cl:
        res = await protected.run(cl, "SUMMARIZATION_LOSS", 2)
        trace = await cl.get_trace(UUID(res.trace_id))
        replay = await cl.replay(UUID(res.trace_id))

    assert res.tool_calls == []
    ((call, reason),) = res.blocked
    assert (call.tool, call.args) == ("issue_refund", {"order": 4471, "amount": 2000})
    assert "requires approval" in reason
    assert "stopped the drifted action" in render_report(res)

    # Complete: every hop's CONSUME and PUBLISH, and the tool gate, have a verdict.
    assert [(v["hop_id"], v["checkpoint"], v["decision"]) for v in trace["verdicts"]] == [
        ("h1", "CONSUME", "ALLOW"), ("h1", "PUBLISH", "ALLOW"),
        ("h2", "CONSUME", "ALLOW"), ("h2", "PUBLISH", "ALLOW"),
        ("h3", "CONSUME", "ALLOW"), ("h3.t1", "TOOL_CALL", "BLOCK"),
        ("h3", "PUBLISH", "ALLOW"),
    ]  # fmt: skip
    assert (trace["status"], trace["outcome"]) == ("closed", "BLOCKED")
    events = await store.read_trace(UUID(res.trace_id))
    counts = Counter(e.type for e in events)
    assert (
        counts["CHECKPOINT_RECEIVED"]
        == counts["VERDICT_ISSUED"]
        == counts["DECISION_ENFORCED"]
        == 7
    )
    assert counts["TRACE_OPENED"] == counts["TRACE_CLOSED"] == 1
    assert [e.event_id for e in events] == sorted(e.event_id for e in events)

    # Replayable: recomputing every verdict from the log reproduces every decision.
    assert replay["changed"] == 0
    assert len(replay["items"]) == 7

    # Projections are a pure function of the log: a rebuild yields identical tables.
    live = await store.projection_rows()
    await store.project_all()
    assert await store.projection_rows() == live
    assert live["verdicts"][0]["trace_id"] == res.trace_id
    # And the in-memory fold agrees with the SQL projection.
    state = fold(events[0].trace_id, [Event.model_validate(e.model_dump()) for e in events])
    assert len(state.verdicts) == len(live["verdicts"]) == 7
    await store.close()


async def test_cli(capsys: pytest.CaptureFixture[str]) -> None:
    await protected._main(
        argparse.Namespace(
            matrix=False, compare=True, mutation="SUMMARIZATION_LOSS", hop=2,
            org_policy=False, base_url=None,
        )
    )  # fmt: skip
    out = capsys.readouterr().out
    assert "Pipeline: no error, no warning" in out  # baseline half
    assert "Pipeline halted by ConstraintLens" in out  # protected half: stopped at triage (T1b)
    assert "limit 100 USD missing" in out
    await protected._main(argparse.Namespace(matrix=True, org_policy=True, base_url=None))
    assert "CONSTRAINT_DROP_ONE" in capsys.readouterr().out
