"""Attribution accuracy on driftchain-bench ``val`` (ARCHITECTURE.md §14.3, §23 Phase 7).

Usage::

    uv run python -m eval.attribution_accuracy                      # print the tables
    uv run python -m eval.attribution_accuracy --out eval/reports   # also write reports

Each chain is replayed through the real use cases (``OpenTrace``, ``VerifyCheckpoint``,
``AttributeTrace``) on an in-memory store with a frozen clock that follows the chain's
consumption times. Hop *k* consumes each parent's envelope (CONSUME, with the recorded
queue dwell and delivery count; a redelivered hop is submitted twice), then its own text
T_k is checked at TOOL_CALL (if it acts) and at PUBLISH (every hop, as ``guard_hop``). Every
checkpoint is submitted whatever the verdict ("observe-only"): the benchmark text is a
recording, so a blocked agent cannot be made to retry. The constraints are registered
from the record's specs, not re-extracted (``extract=False``), so extraction errors do
not leak into attribution.

**Metric (§4.4, §14.3).** For a drifted chain, the predicted culprit is the earliest
hop named by any attribution (hops are numbered in topological order). It is correct
when it equals ``label.mutated_hop``. Also reported: top-1 agent accuracy (a queue-edge
culprit has no agent, which is correct for ``STALE_CONTEXT``), cause accuracy, accuracy
conditional on the chain being flagged at all, per-constraint accuracy on the violated
constraints, and the share of control chains that receive any attribution.

**Detectors.** Only rules run on this host (no NLI weights, no judge key), so:

* ``rules``: the production Tiers 0, 1a, 1b. This is the system result.
* ``oracle-text``: Tiers 0 and 1a plus a tier that reads the benchmark's own
  reference features (``driftchain_bench.reference``). It detects every textual
  mutation exactly, so it isolates the attribution algorithm from detection. It is an
  upper bound for analysis, **not** a system result.

**Dwell slices.** Staleness (§9.3) uses the service's dwell threshold (default 300 s,
to be tuned on ``val`` in Phase 8) and the constraints' 6-hour age bound. Without a
semantic tier nothing can re-verify a stale constraint, so the 300 s default also
reports the first long queue edge. The ``age-only`` slice sets the dwell threshold to
the 6-hour bound to show that effect. Neither value is chosen here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from driftchain_bench.generator import MAX_AGE_S, read_jsonl
from driftchain_bench.reference import features
from driftchain_bench.scenarios import load_scenarios

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from constraintlens.domain.checker.pipeline import CheckContext, Tier
    from constraintlens.domain.models import Attribution
    from driftchain_bench.record import ChainRec

DATA = Path("driftchain_bench/data/driftchain-bench-v0.1.0-seed7-n4000.jsonl")
T_ISSUE = datetime(2026, 1, 1, tzinfo=UTC)
DEFAULT_DWELL_S = 300.0  # Settings.dwell_threshold_s
SLICES: dict[str, float] = {"default-dwell": DEFAULT_DWELL_S, "age-only": float(MAX_AGE_S)}
EXPECTED_CAUSE = {"STALE_CONTEXT": "STALE_CONTEXT", "INJECTED_INSTRUCTION": "INJECTION"}


# --------------------------------------------------------------------------- #
# Oracle detector (analysis only)
# --------------------------------------------------------------------------- #


class OracleTextTier:
    """Flags a constraint when T_k's reference features differ from T_0's.

    Reads the benchmark's own feature extractor, so it is exact on template text. It
    gives no scores and does not re-verify staleness (``semantic = False``).
    """

    name: TierId = "T1b"

    def __init__(self, chain: ChainRec, unit: str, source: str) -> None:
        self.unit = unit
        self.source = source
        self.ids = _allowed_ids(chain)
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


def _allowed_ids(chain: ChainRec) -> list[str]:
    subject = next(c.spec.subject for c in chain.constraints if c.scope == "user")
    return re.findall(r":([^,{}]+)", subject)


# --------------------------------------------------------------------------- #
# Running one chain
# --------------------------------------------------------------------------- #


def _spec(chain: ChainRec, i: int) -> ConstraintSpec:
    return ConstraintSpec.model_validate(chain.constraints[i].spec.model_dump())


def _binding(chain: ChainRec, tool: str) -> ActionBinding:
    return ActionBinding.model_validate(chain.tools[tool].model_dump())


@dataclass
class ChainResult:
    chain: ChainRec
    attributions: list[Attribution]
    ids: dict[str, UUID]  # record constraint id -> registered id

    @property
    def predicted(self) -> Attribution | None:
        """The earliest named culprit (hops are numbered topologically; root is 0)."""
        named = [a for a in self.attributions if a.culprit_hop is not None]
        return min(named, key=lambda a: _hop_number(a.culprit_hop or ""), default=None)

    def for_constraint(self, cid: str) -> Attribution | None:
        return next((a for a in self.attributions if a.constraint_id == self.ids[cid]), None)


def _hop_number(hop: str) -> int:
    return 0 if hop == "root" else int(hop.removeprefix("h"))


async def run_chain(chain: ChainRec, *, detector: str, dwell_s: float) -> ChainResult:
    store, clock = InMemoryStore(), FrozenClock(T_ISSUE)
    words = load_scenarios()[chain.scenario].words
    unit = next(c.spec.limit.unit for c in chain.constraints if c.spec.limit)
    tiers: list[Tier] = [IntegrityTier(), ActionTier()]
    tiers.append(
        LexicalTier() if detector == "rules" else OracleTextTier(chain, unit, words["source"])
    )
    guard = GuardConfig(dwell_threshold_s=dwell_s)
    verify = VerifyCheckpoint(
        store=store, clock=clock, checker=TieredChecker(tiers, clock), policy=Policy(),
        guard=guard,
    )  # fmt: skip
    opened = await OpenTrace(store=store, clock=clock, llm_mode="off", default_max_age_s=MAX_AGE_S)(
        OpenTraceRequest(
            chain.instruction,
            owner=chain.constraints[0].spec.owner,
            declared=[_spec(chain, i) for i in range(len(chain.constraints))],
            extract=False,
        )
    )
    ids = {rec.id: c.constraint_id for rec, c in zip(chain.constraints, opened.constraints,
                                                     strict=True)}  # fmt: skip
    trace_id = opened.trace_id
    envelopes: dict[str, Envelope] = {"root": opened.envelope}

    async def submit(**kw: Any) -> None:
        await verify(CheckpointRequest(trace_id=trace_id, **kw))

    for hop in sorted(chain.hops, key=lambda h: h.arrival):
        target = T_ISSUE + timedelta(seconds=hop.consumed_at_s)
        clock.advance(max(0.0, (target - clock.now()).total_seconds()))
        hop_id = f"h{hop.hop}"
        parents = [f"h{p}" for p in hop.parents] or ["root"]
        enqueued = target - timedelta(seconds=hop.queue_dwell_s)
        for parent in parents:
            for delivery in range(1, hop.delivery + 1):  # at-least-once: redelivered twice
                await submit(
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
        if hop.action is not None:
            await submit(
                hop_id=f"{hop_id}.t1", checkpoint="TOOL_CALL", envelope=env,
                proposed_action=ProposedAction(
                    tool=hop.action.tool, args=dict(hop.action.args),
                    binding=_binding(chain, hop.action.tool),
                ),
            )  # fmt: skip
        # Like the SDK's guard_hop: every agent publishes its output, sinks included.
        await submit(hop_id=hop_id, checkpoint="PUBLISH", envelope=env)

    report = await AttributeTrace(store=store, clock=clock, params=AttributionParams(guard=guard))(
        trace_id
    )
    return ChainResult(chain, report.attributions, ids)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


@dataclass
class Tally:
    n: int = 0
    flagged: int = 0
    hop: int = 0
    agent: int = 0
    cause: int = 0  # cause correct, among hop-correct

    def add(self, r: ChainResult) -> None:
        lab = r.chain.label
        self.n += 1
        pred = r.predicted
        if pred is None:
            return
        self.flagged += 1
        if pred.culprit_hop != f"h{lab.mutated_hop}":
            return
        self.hop += 1
        op = lab.mutation_op
        expected_agent = None if op == "STALE_CONTEXT" else r.chain.hops[lab.mutated_hop - 1].agent
        self.agent += pred.culprit_agent == expected_agent
        self.cause += pred.cause == EXPECTED_CAUSE.get(op, "REWRITE")

    def row(self) -> dict[str, Any]:
        def rate(k: int, d: int) -> float | None:
            return round(k / d, 4) if d else None

        return {
            "n": self.n,
            "flagged": rate(self.flagged, self.n),
            "hop_accuracy": rate(self.hop, self.n),
            "hop_accuracy_if_flagged": rate(self.hop, self.flagged),
            "agent_accuracy": rate(self.agent, self.n),
            "cause_accuracy_if_hop_correct": rate(self.cause, self.hop),
        }


@dataclass
class SliceReport:
    overall: Tally = field(default_factory=Tally)
    by_operator: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    by_topology: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    violated_n: int = 0
    violated_hop: int = 0
    controls: int = 0
    controls_attributed: int = 0
    causes: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, r: ChainResult) -> None:
        lab = r.chain.label
        if not lab.drift:
            self.controls += 1
            if r.attributions:
                self.controls_attributed += 1
                for a in r.attributions:
                    self.causes[a.cause] += 1
            return
        self.overall.add(r)
        self.by_operator[lab.mutation_op].add(r)
        self.by_topology[r.chain.topology].add(r)
        for cid in lab.violated_constraints:
            a = r.for_constraint(cid)
            self.violated_n += 1
            self.violated_hop += a is not None and a.culprit_hop == f"h{lab.mutated_hop}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "drifted": self.overall.row(),
            "by_operator": {k: v.row() for k, v in sorted(self.by_operator.items())},
            "by_topology": {k: v.row() for k, v in sorted(self.by_topology.items())},
            "violated_constraints": {
                "n": self.violated_n,
                "hop_accuracy": round(self.violated_hop / self.violated_n, 4)
                if self.violated_n
                else None,
            },
            "controls": {
                "n": self.controls,
                "with_any_attribution": round(self.controls_attributed / self.controls, 4)
                if self.controls
                else None,
                "causes": dict(sorted(self.causes.items())),
            },
        }


async def evaluate(chains: Sequence[ChainRec]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for detector in ("rules", "oracle-text"):
        for slice_name, dwell in SLICES.items():
            rep = SliceReport()
            for chain in chains:
                rep.add(await run_chain(chain, detector=detector, dwell_s=dwell))
            results[f"{detector}/{slice_name}"] = {
                "detector": detector,
                "dwell_threshold_s": dwell,
                **rep.as_dict(),
            }
    return results


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Attribution accuracy on driftchain-bench `val` (Phase 7)",
        "",
        f"Benchmark `{report['benchmark']}`, split `{report['split']}`: "
        f"{report['chains']} chains ({report['drifted']} drifted, "
        f"{report['chains'] - report['drifted']} controls). Generated {report['generated_at']}.",
        "",
        "Correct = the earliest culprit hop named equals `label.mutated_hop` (§4.4). "
        "`rules` is the system with the tiers available on this host (T0, T1a, T1b; no NLI "
        "model, no judge). `oracle-text` swaps T1b for the benchmark's reference features, "
        "an exact text detector, to isolate the attribution algorithm: an upper bound, "
        "not a system result. theta = 0.35, delta_min = 0.15 (defaults, untuned).",
        "",
        "## Summary",
        "",
        "| Detector / slice | dwell threshold | flagged | hop acc. | hop acc. if flagged "
        "| agent acc. | cause acc. (hop correct) | violated-constraint hop acc. "
        "| controls with any attribution |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, r in report["results"].items():
        d = r["drifted"]
        lines.append(
            f"| `{name}` | {r['dwell_threshold_s']:.0f} s | {_pct(d['flagged'])} "
            f"| **{_pct(d['hop_accuracy'])}** | {_pct(d['hop_accuracy_if_flagged'])} "
            f"| {_pct(d['agent_accuracy'])} | {_pct(d['cause_accuracy_if_hop_correct'])} "
            f"| {_pct(r['violated_constraints']['hop_accuracy'])} "
            f"| {_pct(r['controls']['with_any_attribution'])} |"
        )
    for name, r in report["results"].items():
        lines += [
            "",
            f"## `{name}` by operator",
            "",
            "| Operator | n | flagged | hop acc. | hop acc. if flagged | cause acc. |",
            "|---|---|---|---|---|---|",
        ]
        for op, row in r["by_operator"].items():
            lines.append(
                f"| {op} | {row['n']} | {_pct(row['flagged'])} | {_pct(row['hop_accuracy'])} "
                f"| {_pct(row['hop_accuracy_if_flagged'])} "
                f"| {_pct(row['cause_accuracy_if_hop_correct'])} |"
            )
        lines += ["", "| Topology | n | hop acc. | hop acc. if flagged |", "|---|---|---|---|"]
        for topo, row in r["by_topology"].items():
            lines.append(
                f"| {topo} | {row['n']} | {_pct(row['hop_accuracy'])} "
                f"| {_pct(row['hop_accuracy_if_flagged'])} |"
            )
        causes = ", ".join(f"{k} {v}" for k, v in r["controls"]["causes"].items()) or "none"
        lines += ["", f"Attributions on control chains, by cause: {causes}."]
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", type=Path, default=DATA, help="benchmark JSONL")
    parser.add_argument("--split", default="val", choices=["dev", "val"])
    parser.add_argument("--limit", type=int, default=0, help="first N chains only (debugging)")
    parser.add_argument("--out", type=Path, default=None, help="write report files here")
    args = parser.parse_args(argv)
    if not args.data.exists():
        parser.error(f"{args.data} not found; run `make bench` first")
    chains = [c for c in read_jsonl(args.data) if c.split == args.split]
    if args.limit:
        chains = chains[: args.limit]
    results = asyncio.run(evaluate(chains))
    report = {
        "benchmark": args.data.name,
        "split": args.split,
        "chains": len(chains),
        "drifted": sum(c.label.drift for c in chains),
        "params": {"theta": 0.35, "delta_min": 0.15},
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "results": results,
    }
    md = render_markdown(report)
    print(md)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        stem = f"attribution_{args.split}_v1"
        (args.out / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n", "utf-8")
        (args.out / f"{stem}.md").write_text(md, "utf-8")


if __name__ == "__main__":
    main()
