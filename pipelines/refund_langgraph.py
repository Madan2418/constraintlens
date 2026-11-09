"""Protected refund pipeline: the baseline agents, with ConstraintLens (§2.1, §23 Phase 3).

Same scripted agents, same drift, same tools as ``pipelines/baseline/refund_langgraph.py``.
The only difference is the SDK:

* the trace is opened with the user's instruction (constraints extracted and registered);
  the org policy, when enabled, is *declared* (§7.1), not extracted;
* every agent node is wrapped by ``guard_hop`` (CONSUME before, PUBLISH after);
* every tool goes through ``guard_tool`` (TOOL_CALL gate). A blocked tool does not run.

Run (in-process service with an in-memory store unless ``--base-url`` is given)::

    uv run python -m pipelines.refund_langgraph --mutation SUMMARIZATION_LOSS --hop 2
    uv run python -m pipelines.refund_langgraph --mutation SUMMARIZATION_LOSS --hop 2 --compare
    uv run python -m pipelines.refund_langgraph --matrix
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import TYPE_CHECKING, Any, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

from constraintlens.adapters.pipeline.langgraph import LangGraphStateAdapter
from constraintlens.domain.models import ActionBinding, ConstraintSpec, Limit
from constraintlens.sdk import ConstraintLens, ConstraintViolation, current_hop
from pipelines.baseline import refund_langgraph as baseline
from pipelines.common.agents import HopRecord, RunResult, VirtualClock, agent_step, render_report
from pipelines.common.facts import MUTATIONS, Mutation
from pipelines.common.scenarios import REFUND, Scenario, ToolLedger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

OWNER = "user:customer-118"
ORG_POLICY = ConstraintSpec(
    scope="org",
    owner="org:acme-support",
    origin="policy",
    action="refund",
    resource="order",
    limit=Limit(max_value=Decimal(50), unit="USD"),
)


class State(TypedDict):
    text: str
    cl_envelope: dict[str, Any]
    hops: list[HopRecord]


class GuardedTools:
    """The final agent's tools, each behind the TOOL_CALL gate."""

    def __init__(
        self, cl: ConstraintLens, scenario: Scenario, ledger: ToolLedger, hop: int, agent: str
    ) -> None:
        self.cl, self.scenario, self.ledger, self.hop, self.agent = cl, scenario, ledger, hop, agent

    async def __call__(self, tool: str, **args: Any) -> None:
        raw = self.scenario.tool_bindings.get(tool)
        binding = ActionBinding(**raw) if raw else None

        @self.cl.guard_tool(name=tool, binding=binding)
        async def execute(**call_args: Any) -> None:
            self.ledger.record(tool, self.hop, self.agent, **call_args)

        try:
            await execute(**args)
        except ConstraintViolation as exc:
            self.ledger.record_blocked(tool, self.hop, self.agent, exc.verdict.reason, **args)
            raise


def build_graph(
    cl: ConstraintLens,
    *,
    scenario: Scenario,
    mutation: Mutation,
    mutated_hop: int | None,
    clock: VirtualClock,
    ledger: ToolLedger,
) -> Any:
    adapter = LangGraphStateAdapter()

    def make_agent(hop: int, *, dwell_s: float = 0.0, final: bool = False) -> Any:
        role = scenario.roles[hop - 1]

        @cl.guard_hop(agent_id=role, adapter=adapter, hop_id=lambda _env: f"h{hop}")
        async def agent(state: State) -> State:
            if dwell_s:
                clock.advance(dwell_s)
            facts, text, drifted = agent_step(
                scenario, hop, state["text"], mutation=mutation, mutated_hop=mutated_hop
            )
            if final:
                assert current_hop() is not None  # guard_hop sets it
                # A blocked call is recorded in the ledger; the agent's remaining actions
                # are abandoned, as an agent whose tool raised would.
                with contextlib.suppress(ConstraintViolation):
                    await scenario.act(facts, GuardedTools(cl, scenario, ledger, hop, role))
            record = HopRecord(hop, role, text, clock.now(), queue_dwell_s=dwell_s, mutated=drifted)
            return {**state, "text": text, "hops": [*state["hops"], record]}

        return agent

    graph = StateGraph(State)
    graph.add_node("intake", make_agent(1))
    graph.add_node("triage", make_agent(2, dwell_s=baseline.QUEUE_DWELL_S))
    graph.add_node("payments", make_agent(3, final=True))
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "triage")
    graph.add_edge("triage", "payments")
    graph.add_edge("payments", END)
    return graph.compile()


async def run(
    cl: ConstraintLens,
    mutation: Mutation = "NONE",
    mutated_hop: int | None = None,
    *,
    org_policy: bool = False,
    scenario: Scenario = REFUND,
) -> RunResult:
    if mutation != "NONE" and mutated_hop not in (1, 2, 3):
        raise ValueError("a mutation needs --hop 1, 2 or 3")
    clock, ledger = VirtualClock(), ToolLedger()
    opened = await cl.open_trace(
        scenario.instruction(org_policy=False),  # the user's words; policy is declared
        owner=OWNER,
        declared=[ORG_POLICY] if org_policy else [],
    )
    instruction = scenario.instruction(org_policy=org_policy)  # what the agents are given
    result = RunResult(
        pipeline="langgraph+constraintlens",
        scenario=scenario.name,
        trace_id=str(opened.trace_id),
        instruction=instruction,
        mutation=mutation,
        mutated_hop=mutated_hop if mutation != "NONE" else None,
        org_policy=org_policy,
    )
    app = build_graph(
        cl, scenario=scenario, mutation=mutation, mutated_hop=mutated_hop, clock=clock,
        ledger=ledger,
    )  # fmt: skip
    state: State = {
        "text": instruction,
        "cl_envelope": opened.envelope.model_dump(mode="json"),
        "hops": [],
    }
    try:
        final = await app.ainvoke(state)
        result.hops = final["hops"]
    except ConstraintViolation as exc:
        result.halted = exc.verdict.reason
    await cl.close_trace(opened.trace_id)
    result.tool_calls = list(ledger.calls)
    result.blocked = list(ledger.blocked)
    result.violations = scenario.oracle(ledger, org_policy)
    return result


@asynccontextmanager
async def client(base_url: str | None = None) -> AsyncIterator[ConstraintLens]:
    """A client for ``base_url``, or for an in-process service with an in-memory store."""
    if base_url:
        async with ConstraintLens(base_url) as cl:
            yield cl
        return
    from constraintlens.adapters.store.sqlite import SQLiteEventStore
    from constraintlens.api.app import create_app
    from constraintlens.api.wiring import Container
    from constraintlens.config import Settings

    store = await SQLiteEventStore("sqlite:///:memory:").initialize()
    app = create_app(container=Container.build(Settings(_env_file=None), store=store))
    transport = httpx.ASGITransport(app=app)
    async with ConstraintLens("http://constraintlens.local", transport=transport) as cl:
        yield cl
    await store.close()


async def _matrix(base_url: str | None, org_policy: bool) -> str:
    rows = [
        "| mutation | baseline (no ConstraintLens) | with ConstraintLens |",
        "|---|---|---|",
    ]
    async with client(base_url) as cl:
        for m in MUTATIONS:
            if m == "CONSTRAINT_DROP_ONE" and not org_policy:
                continue
            hop = None if m == "NONE" else 2
            before = await baseline.run(m, hop, org_policy=org_policy)
            after = await run(cl, m, hop, org_policy=org_policy)
            rows.append(f"| {m} | {_cell(before)} | {_cell(after)} |")
    return "\n".join(rows)


def _cell(r: RunResult) -> str:
    if r.violations:
        return "VIOLATION executed: " + "; ".join(r.violations)
    if r.blocked:
        call, _ = r.blocked[0]
        return f"blocked {call.tool}({', '.join(f'{k}={v}' for k, v in call.args.items())})"
    return "ok"


async def _main(args: argparse.Namespace) -> None:
    if args.matrix:
        print(await _matrix(args.base_url, args.org_policy))
        return
    if args.compare:
        print(
            render_report(await baseline.run(args.mutation, args.hop, org_policy=args.org_policy))
        )
        print("\n" + "=" * 100 + "\n")
    async with client(args.base_url) as cl:
        print(render_report(await run(cl, args.mutation, args.hop, org_policy=args.org_policy)))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--mutation", choices=MUTATIONS, default="NONE")
    parser.add_argument("--hop", type=int, choices=(1, 2, 3), default=None)
    parser.add_argument("--org-policy", action="store_true", help="declare the $50 org policy")
    parser.add_argument("--compare", action="store_true", help="run the unprotected baseline first")
    parser.add_argument(
        "--matrix", action="store_true", help="every mutation, baseline vs protected"
    )
    parser.add_argument("--base-url", default=None, help="a running ConstraintLens service")
    asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    main()
