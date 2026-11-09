"""Baseline (unprotected) refund pipeline in LangGraph (ARCHITECTURE.md §2.1, §23 Phase 2).

    Intake --(queue, 12 min dwell)--> Triage --> Payments --> issue_refund

Direct, in-process handoff between graph nodes; the queue dwell is simulated on a
virtual clock. No ConstraintLens anywhere: this is the "before" half of the demo.

Run::

    uv run python -m pipelines.baseline.refund_langgraph --mutation SUMMARIZATION_LOSS --hop 2
    uv run python -m pipelines.baseline.refund_langgraph --matrix
"""

from __future__ import annotations

import argparse
import asyncio
import operator
import uuid
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from pipelines.common.agents import HopRecord, RunResult, VirtualClock, agent_step, render_report
from pipelines.common.facts import MUTATIONS, Mutation
from pipelines.common.scenarios import REFUND, LedgerTools, Scenario, ToolLedger

QUEUE_DWELL_S = 12 * 60  # intake -> triage, as in the worked example


class RefundState(TypedDict):
    text: str  # the task text as it travels between agents
    hops: Annotated[list[HopRecord], operator.add]


def build_graph(
    *,
    scenario: Scenario,
    mutation: Mutation,
    mutated_hop: int | None,
    clock: VirtualClock,
    ledger: ToolLedger,
) -> Any:  # LangGraph's CompiledStateGraph generics add noise and no safety here
    # Typed as Any: LangGraph's node protocol (_Node) is private and rejects a plain
    # Callable annotation, although the function matches it at runtime.
    def make_agent(hop: int, *, dwell_s: float = 0.0, final: bool = False) -> Any:
        role = scenario.roles[hop - 1]

        async def node(state: RefundState) -> dict[str, object]:
            if dwell_s:
                clock.advance(dwell_s)
            facts, text, drifted = agent_step(
                scenario, hop, state["text"], mutation=mutation, mutated_hop=mutated_hop
            )
            if final:
                await scenario.act(facts, LedgerTools(ledger, hop, role))
            record = HopRecord(
                hop=hop,
                agent=role,
                text=text,
                emitted_at=clock.now(),
                queue_dwell_s=dwell_s,
                mutated=drifted,
            )
            return {"text": text, "hops": [record]}

        return node

    graph = StateGraph(RefundState)
    graph.add_node("intake", make_agent(1))
    graph.add_node("triage", make_agent(2, dwell_s=QUEUE_DWELL_S))
    graph.add_node("payments", make_agent(3, final=True))
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "triage")
    graph.add_edge("triage", "payments")
    graph.add_edge("payments", END)
    return graph.compile()


async def run(
    mutation: Mutation = "NONE",
    mutated_hop: int | None = None,
    *,
    org_policy: bool = False,
    scenario: Scenario = REFUND,
) -> RunResult:
    if mutation != "NONE" and mutated_hop not in (1, 2, 3):
        raise ValueError("a mutation needs --hop 1, 2 or 3")
    clock, ledger = VirtualClock(), ToolLedger()
    instruction = scenario.instruction(org_policy=org_policy)
    app = build_graph(
        scenario=scenario, mutation=mutation, mutated_hop=mutated_hop, clock=clock, ledger=ledger
    )
    result = RunResult(
        pipeline="langgraph",
        scenario=scenario.name,
        trace_id=str(uuid.uuid4()),
        instruction=instruction,
        mutation=mutation,
        mutated_hop=mutated_hop if mutation != "NONE" else None,
        org_policy=org_policy,
    )
    final = await app.ainvoke({"text": instruction, "hops": []})
    result.hops = final["hops"]
    result.tool_calls = list(ledger.calls)
    result.violations = scenario.oracle(ledger, org_policy)
    return result


def _matrix(org_policy: bool) -> str:
    rows = ["| mutation | hop | executed tool calls | ground truth |", "|---|---|---|---|"]
    for m in MUTATIONS:
        if m == "CONSTRAINT_DROP_ONE" and not org_policy:
            continue
        res = asyncio.run(run(m, None if m == "NONE" else 2, org_policy=org_policy))
        calls = "; ".join(
            f"{c.tool}({', '.join(f'{k}={v}' for k, v in c.args.items())})" for c in res.tool_calls
        )
        verdict = "SILENT VIOLATION" if res.silent_violation else "ok"
        rows.append(f"| {m} | {res.mutated_hop or '-'} | {calls} | {verdict} |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--mutation", choices=MUTATIONS, default="NONE")
    parser.add_argument("--hop", type=int, choices=(1, 2, 3), default=None)
    parser.add_argument("--org-policy", action="store_true", help="add the $50 org policy")
    parser.add_argument("--matrix", action="store_true", help="run every mutation at hop 2")
    args = parser.parse_args(argv)
    if args.matrix:
        print(_matrix(args.org_policy))
        return
    print(render_report(asyncio.run(run(args.mutation, args.hop, org_policy=args.org_policy))))


if __name__ == "__main__":
    main()
