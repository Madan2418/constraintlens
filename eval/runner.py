"""Replay benchmark chains through ConstraintLens under a configuration (§14.2, §14.4).

One function, ``run_chain(chain, config)``, used by every evaluation script:

* the constraints are registered from the record's specs (``extract=False``), so
  extraction errors do not leak into detection;
* a frozen service clock follows the chain's consumption times, while tier latency is
  measured on the real monotonic clock (``HybridClock``);
* hop *k* consumes each parent's envelope (CONSUME with the recorded queue dwell and
  delivery count; a redelivered hop is submitted twice), then its own text T_k is
  checked at TOOL_CALL (if it acts) and at PUBLISH, as the SDK's ``guard_hop`` does;
* every checkpoint the configuration covers is submitted whatever the verdict
  ("observe-only"): the chain text is a recording, so a blocked agent cannot retry. The
  metrics decide afterwards which verdicts would have stopped the pipeline.

The configurations themselves live in ``eval/configs``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from constraintlens.adapters.clock import FrozenClock
from constraintlens.adapters.store.memory import InMemoryStore
from constraintlens.application.attribute_trace import AttributeTrace
from constraintlens.application.open_trace import OpenTrace, OpenTraceRequest
from constraintlens.application.verify_checkpoint import VerifyCheckpoint
from constraintlens.domain.async_guard import GuardConfig
from constraintlens.domain.attribution import AttributionParams
from constraintlens.domain.checker.pipeline import Finding, TieredChecker, TierOutcome
from constraintlens.domain.checker.tier0_integrity import IntegrityTier
from constraintlens.domain.checker.tier1a_action import ActionTier
from constraintlens.domain.checker.tier1b_lexical import LexicalTier
from constraintlens.domain.enforcement import Policy
from constraintlens.domain.models import (
    ActionBinding,
    CheckpointRequest,
    ConstraintSpec,
    Envelope,
    Failure,
    HopMeta,
    ProposedAction,
    TierId,
    TransportMeta,
)
from driftchain_bench.generator import MAX_AGE_S
from driftchain_bench.reference import features
from driftchain_bench.scenarios import load_scenarios

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from constraintlens.domain.checker.pipeline import CheckContext, Tier
    from constraintlens.domain.models import Attribution, Verdict
    from driftchain_bench.record import ChainRec
    from eval.configs import EvalConfig

T_ISSUE = datetime(2026, 1, 1, tzinfo=UTC)
DEFAULT_DWELL_S = 300.0  # Settings.dwell_threshold_s


class HybridClock(FrozenClock):
    """Service time follows the chain; ``monotonic`` is real, so tier latency is real."""

    def monotonic(self) -> float:
        return time.perf_counter()


# --------------------------------------------------------------------------- #
# Oracle text detector (analysis only, never a system result)
# --------------------------------------------------------------------------- #


class OracleTextTier:
    """Flags a constraint when T_k's reference features differ from T_0's.

    Reads the benchmark's own feature extractor, so it is exact on template text. It
    gives no scores and does not re-verify staleness (``semantic = False``).
    """

    name: TierId = "T1b"

    def __init__(self, chain: ChainRec) -> None:
        self.unit = next(c.spec.limit.unit for c in chain.constraints if c.spec.limit)
        self.source = load_scenarios()[chain.scenario].words["source"]
        self.ids = allowed_ids(chain)
        self.base = self._features(chain.instruction)
        user = [c for c in chain.constraints if c.scope == "user"]
        org = [c for c in chain.constraints if c.scope == "org"]
        self.user_limit = user[0].spec.limit.max_value if user and user[0].spec.limit else None
        self.org_cap = org[0].spec.limit.max_value if org and org[0].spec.limit else None

    def _features(self, text: str) -> Any:
        return features(text, unit=self.unit, source=self.source, ids=self.ids)

    def applies(self, ctx: CheckContext) -> bool:
        return bool(ctx.registered)

    async def run(self, ctx: CheckContext) -> TierOutcome:
        f = self._features(ctx.request.envelope.payload_text)
        b = self.base
        raised = bool(f.amounts - b.amounts)
        user_drift = (
            f.injected or raised or f.permission != b.permission or f.approval != b.approval
            or f.ids != b.ids or (self.user_limit is not None and self.user_limit not in f.amounts)
        )  # fmt: skip
        org_drift = f.injected or raised or (
            self.org_cap is not None and self.org_cap not in f.amounts
        )  # fmt: skip
        findings = [
            Finding(Failure(kind="TEXTUAL_DRIFT", constraint_id=c.constraint_id,
                            detail="reference features differ from T_0"))
            for c in ctx.registered
            if (user_drift if c.scope == "user" else org_drift)
        ]  # fmt: skip
        return TierOutcome(findings=findings, terminal=bool(findings))


def allowed_ids(chain: ChainRec) -> list[str]:
    subject = next(c.spec.subject for c in chain.constraints if c.scope == "user")
    return re.findall(r":([^,{}]+)", subject)


# --------------------------------------------------------------------------- #
# Running one chain
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Check:
    """One checkpoint verdict, placed on the chain's hop numbering."""

    hop: int  # the hop at which the checkpoint happened (the consumer, for CONSUME)
    source: int  # the hop whose text T_k was checked (the parent, for CONSUME; 0 = T_0)
    checkpoint: str
    delivery: int
    verdict: Verdict


@dataclass
class ChainRun:
    chain: ChainRec
    config: str
    checks: list[Check] = field(default_factory=list)
    attributions: list[Attribution] = field(default_factory=list)
    ids: dict[str, UUID] = field(default_factory=dict)  # record constraint id -> registered

    @property
    def predicted(self) -> Attribution | None:
        """The earliest named culprit (hops are numbered topologically; root is 0)."""
        named = [a for a in self.attributions if a.culprit_hop is not None]
        return min(named, key=lambda a: hop_number(a.culprit_hop or ""), default=None)

    def for_constraint(self, cid: str) -> Attribution | None:
        if cid not in self.ids:
            return None
        return next((a for a in self.attributions if a.constraint_id == self.ids[cid]), None)


def hop_number(hop: str) -> int:
    return 0 if hop == "root" else int(hop.removeprefix("h"))


def build_tiers(names: Sequence[str], chain: ChainRec) -> list[Tier]:
    made: dict[str, Any] = {
        "T0": IntegrityTier,
        "T1a": ActionTier,
        "T1b": LexicalTier,
        "ORACLE": lambda: OracleTextTier(chain),
    }
    unknown = [n for n in names if n not in made]
    if unknown:
        raise ValueError(f"tiers not available in this harness: {unknown}")
    return [made[n]() for n in names]


def _spec(chain: ChainRec, i: int) -> ConstraintSpec:
    return ConstraintSpec.model_validate(chain.constraints[i].spec.model_dump())


def _binding(chain: ChainRec, tool: str) -> ActionBinding:
    return ActionBinding.model_validate(chain.tools[tool].model_dump())


async def run_chain(
    chain: ChainRec, config: EvalConfig, *, dwell_s: float = DEFAULT_DWELL_S
) -> ChainRun:
    run = ChainRun(chain, config.id)
    store, clock = InMemoryStore(), HybridClock(T_ISSUE)
    guard = GuardConfig(dwell_threshold_s=dwell_s, enabled=config.guard)
    keep = [i for i, c in enumerate(chain.constraints) if config.composition or c.scope == "user"]
    opened = await OpenTrace(store=store, clock=clock, llm_mode="off", default_max_age_s=MAX_AGE_S)(
        OpenTraceRequest(
            chain.instruction,
            owner=chain.constraints[0].spec.owner,
            declared=[_spec(chain, i) for i in keep],
            extract=False,
        )
    )
    run.ids = {chain.constraints[i].id: c.constraint_id for i, c in zip(keep, opened.constraints,
                                                                      strict=True)}  # fmt: skip
    if config.checkpoints == "none":
        return run
    verify = VerifyCheckpoint(
        store=store, clock=clock, policy=Policy(), guard=guard,
        checker=TieredChecker(build_tiers(config.tiers, chain), clock),
    )  # fmt: skip
    trace_id = opened.trace_id
    envelopes: dict[str, Envelope] = {"root": opened.envelope}

    def wanted(hop: int, checkpoint: str) -> bool:
        if config.checkpoints == "tool_call":
            return checkpoint == "TOOL_CALL"
        if config.checkpoints == "first_hop":
            return hop == 1 and checkpoint in ("CONSUME", "PUBLISH")
        return True

    async def submit(hop: int, source: int, delivery: int, **kw: Any) -> None:
        verdict = await verify(CheckpointRequest(trace_id=trace_id, **kw))
        run.checks.append(Check(hop, source, kw["checkpoint"], delivery, verdict))

    for hop in sorted(chain.hops, key=lambda h: h.arrival):
        target = T_ISSUE + timedelta(seconds=hop.consumed_at_s)
        clock.advance(max(0.0, (target - clock.now()).total_seconds()))
        hop_id = f"h{hop.hop}"
        parents = [f"h{p}" for p in hop.parents] or ["root"]
        enqueued = target - timedelta(seconds=hop.queue_dwell_s)
        if wanted(hop.hop, "CONSUME"):
            for parent in parents:
                for delivery in range(1, hop.delivery + 1):  # at-least-once: twice
                    await submit(
                        hop.hop, hop_number(parent), delivery,
                        hop_id=hop_id, checkpoint="CONSUME", envelope=envelopes[parent],
                        transport_meta=TransportMeta(
                            message_id=f"{parent}->{hop_id}", delivery_count=delivery,
                            enqueued_at=enqueued, consumed_at=target,
                        ),
                    )  # fmt: skip
        ps = [envelopes[p] for p in parents]
        env = Envelope(
            trace_id=trace_id,
            hop=HopMeta(
                hop_id=hop_id, parent_hop_ids=parents,
                hop_seq=max(p.hop.hop_seq for p in ps) + 1, agent_id=hop.agent,
                emitted_at=target,
            ),
            constraints=list(opened.envelope.constraints),
            envelope_digest=opened.envelope.envelope_digest,
            payload_text=hop.text,
        )  # fmt: skip
        envelopes[hop_id] = env
        if hop.action is not None and wanted(hop.hop, "TOOL_CALL"):
            await submit(
                hop.hop, hop.hop, 1,
                hop_id=f"{hop_id}.t1", checkpoint="TOOL_CALL", envelope=env,
                proposed_action=ProposedAction(
                    tool=hop.action.tool, args=dict(hop.action.args),
                    binding=_binding(chain, hop.action.tool),
                ),
            )  # fmt: skip
        if wanted(hop.hop, "PUBLISH"):
            # Like the SDK's guard_hop: every agent publishes its output, sinks included.
            await submit(hop.hop, hop.hop, 1, hop_id=hop_id, checkpoint="PUBLISH", envelope=env)

    report = await AttributeTrace(store=store, clock=clock, params=AttributionParams(guard=guard))(
        trace_id
    )
    run.attributions = report.attributions
    return run
