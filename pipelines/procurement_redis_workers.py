"""Protected procurement pipeline: baseline Redis workers plus ConstraintLens (§9, Phase 4).

    requester --[stream]--> sourcing --[stream, 45 min dwell]--> purchasing --> place_order

Same scripted agents, same drift, same tools and the same at-least-once failure as
``pipelines/baseline/procurement_redis_workers.py``. The only difference is the SDK:

* the trace is opened with the user's instruction; the org policy, when enabled, is
  declared (§7.1);
* every worker is a ``GuardedWorker``: CONSUME (with the delivery's transport metadata)
  before the agent runs, PUBLISH (naming the destination stream) before it emits;
* every tool goes through ``guard_tool`` (TOOL_CALL gate).

What the async boundary adds (§9):

* **Redelivery** (``--crash-before-ack``): the purchasing worker dies after ordering but
  before acking; a second worker reclaims the message. Its checkpoints get the stored
  verdicts (no conflicting decision), and the re-issued ``place_order`` is recognised as
  already authorized and not executed again. One order, not two.
* **Staleness**: the 45-minute dwell before purchasing exceeds the dwell threshold, so
  the budget constraint must be re-verified by a semantic tier (1c / 2) before the
  purchase proceeds (§9.3). Until those tiers exist (Phase 5), a high-risk constraint
  fails closed and purchasing is halted at CONSUME. ``--dwell-minutes 2`` keeps the
  dwell under the threshold.

The transport is Redis Streams through ``RedisStreamsTransport``: real Redis with
``--redis-url`` / ``REDIS_URL``, otherwise an in-process fakeredis. Queue dwell is
simulated on a virtual clock, which also stamps the stream ids (simulation only).

Run (in-process service unless ``--base-url`` is given)::

    uv run python -m pipelines.procurement_redis_workers --mutation SUMMARIZATION_LOSS --hop 2
    uv run python -m pipelines.procurement_redis_workers --crash-before-ack --dwell-minutes 2
    uv run python -m pipelines.procurement_redis_workers --crash-before-ack --compare
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from constraintlens.adapters.clock import FrozenClock
from constraintlens.adapters.transport.redis_streams import RedisStreamsTransport
from constraintlens.domain.models import ActionBinding, ConstraintSpec, Limit
from constraintlens.sdk import ConstraintLens, ConstraintViolation, GuardedWorker
from pipelines.baseline import procurement_redis_workers as baseline
from pipelines.common.agents import (
    START,
    HopRecord,
    RunResult,
    agent_step,
    attribution_lines,
    render_report,
)
from pipelines.common.facts import MUTATIONS, Mutation
from pipelines.common.scenarios import PROCUREMENT, Scenario, ToolLedger
from pipelines.refund_langgraph import client

if TYPE_CHECKING:
    from constraintlens.sdk import WorkerTask

OWNER = "user:it-lead"
GROUP = "agents"
ORG_POLICY = ConstraintSpec(
    scope="org",
    owner="org:acme-procurement",
    origin="policy",
    action="purchase",
    resource="purchase",
    limit=Limit(max_value=Decimal(10000), unit="USD"),
)
MAX_ROUNDS = 10


class WorkerCrashed(Exception):
    """Simulated worker death after acting, before the ack."""


class GuardedTools:
    """The purchasing agent's tools, each behind the TOOL_CALL gate."""

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


async def run(
    cl: ConstraintLens,
    mutation: Mutation = "NONE",
    mutated_hop: int | None = None,
    *,
    org_policy: bool = False,
    crash_before_ack: bool = False,
    redis_url: str | None = None,
    dwell_s: dict[int, float] | None = None,
    scenario: Scenario = PROCUREMENT,
) -> RunResult:
    if mutation != "NONE" and mutated_hop not in (1, 2, 3):
        raise ValueError("a mutation needs --hop 1, 2 or 3")
    dwell = dwell_s or baseline.DWELL_S
    clock, ledger = FrozenClock(START), ToolLedger()
    opened = await cl.open_trace(
        scenario.instruction(org_policy=False),  # the user's words; policy is declared
        owner=OWNER,
        declared=[ORG_POLICY] if org_policy else [],
    )
    result = RunResult(
        pipeline="redis-workers+constraintlens",
        scenario=scenario.name,
        trace_id=str(opened.trace_id),
        instruction=scenario.instruction(org_policy=org_policy),
        mutation=mutation,
        mutated_hop=mutated_hop if mutation != "NONE" else None,
        org_policy=org_policy,
    )
    redis = await baseline.make_client(redis_url)
    transport = RedisStreamsTransport(redis, clock=clock)
    run_id = uuid.uuid4().hex[:8]
    streams = [f"cl-demo:{run_id}:{role}" for role in scenario.roles]
    crash = {"pending": crash_before_ack}

    def make_handler(hop: int) -> Any:
        role = scenario.roles[hop - 1]
        final = hop == len(scenario.roles)

        async def handler(task: WorkerTask) -> str | None:
            facts, text, drifted = agent_step(
                scenario, hop, task.text, mutation=mutation, mutated_hop=mutated_hop
            )
            result.hops.append(
                HopRecord(
                    hop, role, text, clock.now(),
                    queue_dwell_s=(clock.now() - task.delivery.enqueued_at).total_seconds(),
                    delivery=task.delivery.delivery_count, mutated=drifted,
                )
            )  # fmt: skip
            if not final:
                return text
            # A blocked call is recorded in the ledger; the agent's remaining actions are
            # abandoned, as an agent whose tool raised would.
            with contextlib.suppress(ConstraintViolation):
                await scenario.act(facts, GuardedTools(cl, scenario, ledger, hop, role))
            if crash["pending"]:
                crash["pending"] = False
                raise WorkerCrashed  # acted, never acked
            return None

        return handler

    workers = [
        GuardedWorker(
            cl, transport, stream=streams[i], group=GROUP, consumer=f"{role}-1",
            agent_id=role, out_stream=streams[i + 1] if i + 1 < len(streams) else None,
            clock=clock, reclaim_idle_ms=0,
        )
        for i, role in enumerate(scenario.roles)
    ]  # fmt: skip
    handlers = [make_handler(i + 1) for i in range(len(workers))]
    try:
        for w in workers:
            await w.start()
        # The root task: the registered envelope, carrying the text the agents are given
        # (the user's words plus any org policy sentence). T_0 is not part of the digest.
        root = opened.envelope.model_copy(update={"payload_text": result.instruction})
        await transport.publish(streams[0], root)
        for round_ in range(MAX_ROUNDS):
            progressed = False
            for i, w in enumerate(workers):
                if round_ == 0:
                    clock.advance(dwell[i + 1])  # the message waits in the queue
                try:
                    done = await w.poll(handlers[i])
                except WorkerCrashed:
                    progressed = True
                    result.guard_notes.append(
                        f"{w.agent_id} worker died after acting, before ack; message pending"
                    )
                    w.consumer = f"{w.agent_id}-2"  # a second worker takes over (reclaim)
                    continue
                progressed |= _record(result, done)
            if not progressed:
                break
    finally:
        await redis.delete(*streams)
        await redis.aclose()
    closed = await cl.close_trace(opened.trace_id)
    result.attribution = attribution_lines(closed)
    result.tool_calls = list(ledger.calls)
    result.blocked = list(ledger.blocked)
    result.violations = scenario.oracle(ledger, org_policy)
    return result


def _record(result: RunResult, processed: list[Any]) -> bool:
    for p in processed:
        if p.outcome == "duplicate":
            result.guard_notes.append(
                f"redelivered message at {p.hop_id.split('.')[0]}: checkpoints answered with the "
                "stored verdicts; the tool call was already authorized, so it was not executed "
                "again"
            )
        elif p.outcome == "held":
            result.guard_notes.append(f"{p.hop_id} held: {p.verdict.reason}")
        elif (
            p.outcome == "blocked" and p.verdict is not None and p.verdict.checkpoint != "TOOL_CALL"
        ):
            result.halted = p.verdict.reason
    return bool(processed)


async def _main(args: argparse.Namespace) -> None:
    dwell = None
    if args.dwell_minutes is not None:
        dwell = {**baseline.DWELL_S, 3: args.dwell_minutes * 60.0}
    if args.compare:
        print(
            render_report(
                await baseline.run(
                    args.mutation, args.hop, org_policy=args.org_policy,
                    crash_before_ack=args.crash_before_ack, redis_url=args.redis_url,
                )
            )
        )  # fmt: skip
        print("\n" + "=" * 100 + "\n")
    async with client(args.base_url) as cl:
        res = await run(
            cl, args.mutation, args.hop, org_policy=args.org_policy,
            crash_before_ack=args.crash_before_ack, redis_url=args.redis_url, dwell_s=dwell,
        )  # fmt: skip
    print(render_report(res))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--mutation", choices=MUTATIONS, default="NONE")
    parser.add_argument("--hop", type=int, choices=(1, 2, 3), default=None)
    parser.add_argument("--org-policy", action="store_true", help="declare the $10,000 org policy")
    parser.add_argument(
        "--crash-before-ack",
        action="store_true",
        help="purchasing worker dies after acting; the message is redelivered",
    )
    parser.add_argument(
        "--dwell-minutes", type=float, default=None,
        help="queue dwell before purchasing (default 45, over the 5-minute stale threshold)",
    )  # fmt: skip
    parser.add_argument("--compare", action="store_true", help="run the unprotected baseline first")
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL"),
        help="real Redis (default: in-process fakeredis)",
    )
    parser.add_argument("--base-url", default=None, help="a running ConstraintLens service")
    asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    main()
