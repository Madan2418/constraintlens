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
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from driftchain_bench.generator import MAX_AGE_S, read_jsonl
from eval.configs import get
from eval.metrics import EXPECTED_CAUSE
from eval.runner import DEFAULT_DWELL_S, ChainRun
from eval.runner import run_chain as _run

if TYPE_CHECKING:
    from collections.abc import Sequence

    from driftchain_bench.record import ChainRec

DATA = Path("driftchain_bench/data/driftchain-bench-v0.1.0-seed7-n4000.jsonl")
SLICES: dict[str, float] = {"default-dwell": DEFAULT_DWELL_S, "age-only": float(MAX_AGE_S)}
DETECTORS = {"rules": "R", "oracle-text": "ORACLE"}  # eval/configs ids
ChainResult = ChainRun  # the Phase 7 name, kept for callers


async def run_chain(chain: ChainRec, *, detector: str, dwell_s: float) -> ChainRun:
    """One chain under the ``rules`` (config R) or ``oracle-text`` (ORACLE) detector."""
    return await _run(chain, get(DETECTORS[detector]), dwell_s=dwell_s)


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

    def add(self, r: ChainRun) -> None:
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

    def add(self, r: ChainRun) -> None:
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
