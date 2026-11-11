"""Threshold tuning on ``val`` (ARCHITECTURE.md §13.7, §14.4 step 2, ADR 0010).

Usage::

    uv run python -m eval.tune --out eval/reports   # -> eval/configs/thresholds_provisional.json

Only the parameters that move a result on this host are swept. Without an NLI model or
a judge, every score is derived from findings (ADR 0009), so ``θ_low``, ``θ_high`` and
``δ_min`` change nothing. They stay at their defaults and are listed as untuned. The
**dwell threshold** (§9.3) does move results: a stale constraint that no semantic tier
can re-verify fails closed. It is swept over a fixed grid for configuration ``R``.

**Objective (fixed before the sweep):** maximize detection recall subject to a
false-block rate ≤ 5% on control chains. Ties go to the lower false-block rate, then to
the *smaller* threshold (the more conservative choice). If no value meets the bound,
the one with the lowest false-block rate is chosen and the file says so.

The result is written with ``"status": "provisional"``. Freezing (``"frozen"``), which
unlocks the ``test`` split, needs the human validation labels and the semantic tiers
(ADR 0008, ADR 0010), and is a deliberate manual step.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from driftchain_bench.generator import read_jsonl
from eval.configs import get
from eval.metrics import headline, outcome
from eval.runner import DEFAULT_DWELL_S, run_chain

if TYPE_CHECKING:
    from collections.abc import Sequence

    from driftchain_bench.record import ChainRec

DATA = Path("driftchain_bench/data/driftchain-bench-v0.1.0-seed7-n4000.jsonl")
THRESHOLDS = Path("eval/configs/thresholds_provisional.json")
GRID_S: tuple[float, ...] = (300, 600, 900, 1800, 3600, 7200, 14400, 21600)
MAX_FALSE_BLOCK = 0.05
OBJECTIVE = (
    "maximize detection recall subject to false-block rate <= 0.05 on val controls; "
    "ties: lower false-block rate, then the smaller threshold"
)


async def sweep(chains: Sequence[ChainRec], grid: Sequence[float]) -> list[dict[str, Any]]:
    config = get("R")
    rows = []
    for dwell in grid:
        outs = [outcome(await run_chain(c, config, dwell_s=dwell)) for c in chains]
        rows.append({"dwell_threshold_s": dwell, **headline(outs)})
    return rows


def choose(rows: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    ok = [r for r in rows if (r["false_block_rate"] or 0.0) <= MAX_FALSE_BLOCK]
    if ok:
        best = max(
            ok,
            key=lambda r: (
                r["detection_recall"] or 0.0, -(r["false_block_rate"] or 0.0),
                -r["dwell_threshold_s"],
            ),
        )  # fmt: skip
        return best, True
    return min(rows, key=lambda r: (r["false_block_rate"] or 0.0, r["dwell_threshold_s"])), False


def render(doc: dict[str, Any]) -> str:
    lines = [
        "# Provisional threshold tuning on `val` (Phase 8)",
        "",
        f"Configuration `R` (rules tiers), {doc['chains']} `val` chains. Objective, fixed "
        f"before the sweep: {doc['objective']}.",
        "",
        "| dwell threshold | detection recall | false-block rate | violation prevention "
        "| precision | F1 |",
        "|---|---|---|---|---|---|",
    ]
    for r in doc["sweep"]:
        mark = " **(chosen)**" if r["dwell_threshold_s"] == doc["dwell_threshold_s"] else ""
        lines.append(
            f"| {r['dwell_threshold_s']:.0f} s{mark} | {r['detection_recall']} "
            f"| {r['false_block_rate']} | {r['violation_prevention']} | {r['precision']} "
            f"| {r['f1']} |"
        )
    lines += [
        "",
        f"Status: **{doc['status']}**. Bound met: {doc['bound_met']}. Untuned (no effect "
        "without semantic scores on this host): " + ", ".join(doc["untuned"]) + ".",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--limit", type=int, default=0, help="first N val chains (debugging)")
    parser.add_argument("--out", type=Path, default=None, help="report directory")
    parser.add_argument("--thresholds", type=Path, default=THRESHOLDS)
    args = parser.parse_args(argv)
    if not args.data.exists():
        parser.error(f"{args.data} not found; run `make bench` first")
    chains = [c for c in read_jsonl(args.data) if c.split == "val"]
    if args.limit:
        chains = chains[: args.limit]
    rows = asyncio.run(sweep(chains, GRID_S))
    best, met = choose(rows)
    doc = {
        "status": "provisional",
        "split": "val",
        "config": "R",
        "chains": len(chains),
        "objective": OBJECTIVE,
        "bound_met": met,
        "dwell_threshold_s": best["dwell_threshold_s"],
        "default_dwell_threshold_s": DEFAULT_DWELL_S,
        "theta_low": 0.35,
        "theta_high": 0.80,
        "delta_min": 0.15,
        "untuned": ["theta_low", "theta_high", "delta_min"],
        "sweep": rows,
        "benchmark": args.data.name,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    md = render(doc)
    print(md)
    if not args.limit:
        args.thresholds.parent.mkdir(parents=True, exist_ok=True)
        args.thresholds.write_text(json.dumps(doc, indent=2) + "\n", "utf-8")
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "tuning_val_v1.md").write_text(md, "utf-8")


if __name__ == "__main__":
    main()
