"""Baseline (unprotected) procurement pipeline on plain Redis Streams workers (§13.2, §23 Phase 2).

    requester --[stream]--> sourcing --[stream, 45 min dwell]--> purchasing --> place_order

Each agent is a worker in a consumer group: it reads with XREADGROUP, does its step,
publishes downstream with XADD, then XACKs. Delivery is at-least-once. With
``crash_before_ack`` the purchasing worker dies after acting but before acking; a
second worker reclaims the message (XAUTOCLAIM) and acts again. Without a guard, the
order is placed twice.

Runs against real Redis (``--redis-url`` or ``REDIS_URL``) or, by default, an in-process
fakeredis server. No ConstraintLens anywhere.

Run::

    uv run python -m pipelines.baseline.procurement_redis_workers \
        --mutation SUMMARIZATION_LOSS --hop 2
    uv run python -m pipelines.baseline.procurement_redis_workers --crash-before-ack
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pipelines.common.agents import HopRecord, RunResult, VirtualClock, agent_step, render_report
from pipelines.common.facts import MUTATIONS, Mutation
from pipelines.common.scenarios import PROCUREMENT, LedgerTools, Scenario, ToolLedger

if TYPE_CHECKING:
    from redis.asyncio import Redis

GROUP = "agents"
#: Queue dwell before each hop consumes (seconds). Hop 3 waits 45 minutes.
DWELL_S = {1: 0.0, 2: 30.0, 3: 45 * 60.0}
MAX_ROUNDS = 20


@dataclass
class Worker:
    hop: int
    role: str
    in_stream: str
    out_stream: str | None
    consumer: str


async def make_client(redis_url: str | None) -> Redis:
    if redis_url:
        from redis.asyncio import Redis

        client: Redis = Redis.from_url(redis_url, decode_responses=True)
        await client.ping()
        return client
    import fakeredis

    return fakeredis.FakeAsyncRedis(decode_responses=True)


def _payload(trace_id: str, hop: int, text: str, enqueued_at: datetime) -> dict[str, str]:
    return {
        "trace_id": trace_id,
        "hop": str(hop),
        "text": text,
        "enqueued_at": enqueued_at.isoformat(),
    }


class _PipelineRun:
    """State of one run: the Redis client, virtual clock, tool ledger and result."""

    def __init__(
        self,
        client: Redis,
        scenario: Scenario,
        result: RunResult,
        mutation: Mutation,
        mutated_hop: int | None,
    ) -> None:
        self.client = client
        self.scenario = scenario
        self.result = result
        self.mutation = mutation
        self.mutated_hop = mutated_hop
        self.clock = VirtualClock()
        self.ledger = ToolLedger()

    async def publish(self, stream: str, hop: int, text: str) -> None:
        fields: dict[Any, Any] = _payload(self.result.trace_id, hop, text, self.clock.now())
        await self.client.xadd(stream, fields)

    async def handle(self, w: Worker, fields: dict[str, str], delivery: int) -> None:
        """One worker step: read the task, maybe drift, then publish downstream or act."""
        enqueued_at = datetime.fromisoformat(fields["enqueued_at"])
        facts, text, drifted = agent_step(
            self.scenario,
            w.hop,
            fields["text"],
            mutation=self.mutation,
            mutated_hop=self.mutated_hop,
        )
        if w.out_stream is None:
            await self.scenario.act(facts, LedgerTools(self.ledger, w.hop, w.role))
        else:
            await self.publish(w.out_stream, w.hop, text)
        self.result.hops.append(
            HopRecord(
                hop=w.hop,
                agent=w.role,
                text=text,
                emitted_at=self.clock.now(),
                queue_dwell_s=(self.clock.now() - enqueued_at).total_seconds(),
                delivery=delivery,
                mutated=drifted,
            )
        )

    async def reclaim(self, w: Worker) -> bool:
        """A second worker takes over the dead worker's pending message (at-least-once)."""
        backup = Worker(w.hop, w.role, w.in_stream, w.out_stream, consumer=f"{w.role}-2")
        claimed: Any = await self.client.xautoclaim(
            w.in_stream, GROUP, backup.consumer, min_idle_time=0, start_id="0-0"
        )
        messages: list[tuple[str, dict[str, str]]] = claimed[1]
        for message_id, fields in messages:
            pending: Any = await self.client.xpending_range(
                w.in_stream, GROUP, min=message_id, max=message_id, count=1
            )
            delivery = int(pending[0]["times_delivered"]) if pending else 2
            await self.handle(backup, fields, delivery)
            await self.client.xack(w.in_stream, GROUP, message_id)
        return bool(messages)


async def run(
    mutation: Mutation = "NONE",
    mutated_hop: int | None = None,
    *,
    org_policy: bool = False,
    crash_before_ack: bool = False,
    redis_url: str | None = None,
    scenario: Scenario = PROCUREMENT,
) -> RunResult:
    if mutation != "NONE" and mutated_hop not in (1, 2, 3):
        raise ValueError("a mutation needs --hop 1, 2 or 3")
    client = await make_client(redis_url)
    trace_id = str(uuid.uuid4())
    streams = [f"cl-demo:{trace_id[:8]}:{role}" for role in scenario.roles]
    workers = [
        Worker(
            hop=i + 1,
            role=role,
            in_stream=streams[i],
            out_stream=streams[i + 1] if i + 1 < len(streams) else None,
            consumer=f"{role}-1",
        )
        for i, role in enumerate(scenario.roles)
    ]
    instruction = scenario.instruction(org_policy=org_policy)
    result = RunResult(
        pipeline="redis-workers",
        scenario=scenario.name,
        trace_id=trace_id,
        instruction=instruction,
        mutation=mutation,
        mutated_hop=mutated_hop if mutation != "NONE" else None,
        org_policy=org_policy,
    )
    state = _PipelineRun(client, scenario, result, mutation, mutated_hop)
    try:
        for s in streams:
            await client.xgroup_create(s, GROUP, id="0", mkstream=True)
        await state.publish(streams[0], 0, instruction)

        for _ in range(MAX_ROUNDS):
            progressed = crashed = False
            for w in workers:
                delivered: Any = await client.xreadgroup(
                    GROUP, w.consumer, {w.in_stream: ">"}, count=1
                )
                for _stream, messages in delivered:
                    for message_id, fields in messages:
                        progressed = True
                        state.clock.advance(DWELL_S[w.hop])
                        await state.handle(w, fields, delivery=1)
                        if crash_before_ack and w.out_stream is None:
                            crashed, crash_before_ack = True, False  # acted, never acked
                            continue
                        await client.xack(w.in_stream, GROUP, message_id)
            if crashed:
                progressed |= await state.reclaim(workers[-1])
            if not progressed:
                break
        else:  # pragma: no cover - defensive: the pipeline is linear and finite
            result.errors.append("pipeline did not settle")
    finally:
        await client.delete(*streams)
        await client.aclose()

    result.tool_calls = list(state.ledger.calls)
    result.violations = scenario.oracle(state.ledger, org_policy)
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--mutation", choices=MUTATIONS, default="NONE")
    parser.add_argument("--hop", type=int, choices=(1, 2, 3), default=None)
    parser.add_argument("--org-policy", action="store_true", help="add the $10,000 org policy")
    parser.add_argument(
        "--crash-before-ack",
        action="store_true",
        help="purchasing worker dies after acting; message is redelivered",
    )
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL"),
        help="real Redis (default: in-process fakeredis)",
    )
    args = parser.parse_args(argv)
    result = asyncio.run(
        run(
            args.mutation,
            args.hop,
            org_policy=args.org_policy,
            crash_before_ack=args.crash_before_ack,
            redis_url=args.redis_url,
        )
    )
    print(render_report(result))
    print(json.dumps({"silent_violation": result.silent_violation, "trace_id": result.trace_id}))


if __name__ == "__main__":
    main()
