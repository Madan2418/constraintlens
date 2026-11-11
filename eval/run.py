"""Run the evaluation (ARCHITECTURE.md §14, §23 Phase 8; ``make eval``).

Usage::

    uv run python -m eval.run --split val --out eval/reports      # all runnable configs
    uv run python -m eval.run --split val --configs R B3          # a subset

Every configuration of §14.2 is listed. Those whose models are missing on this host
(NLI for C1, a judge for C2) are reported as **not run**, with the reason. Each
runnable configuration replays every chain of the split (``eval/runner.py``), computes
the §14.3 metrics per chain (``eval/metrics.py``) and bootstraps 95% intervals over
chains (``eval/stats.py``). Thresholds come from the tuning file
(``eval/configs/thresholds_provisional.json``, written by ``eval.tune``).

**The test split is locked** (§14.4 steps 2-3). ``--split test`` refuses to run unless
``eval/configs/thresholds.json`` exists with ``"status": "frozen"``. Every test run is
appended to ``eval/reports/test_ledger.json``, and a configuration already in the
ledger is refused: test is touched once per configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from driftchain_bench.generator import read_jsonl
from eval.configs import CONFIGS, get
from eval.figures import bar_chart
from eval.metrics import outcome, summary
from eval.runner import DEFAULT_DWELL_S, run_chain
from eval.stats import bootstrap

if TYPE_CHECKING:
    from collections.abc import Sequence

    from driftchain_bench.record import ChainRec
    from eval.configs import EvalConfig

DATA = Path("driftchain_bench/data/driftchain-bench-v0.1.0-seed7-n4000.jsonl")
PROVISIONAL = Path("eval/configs/thresholds_provisional.json")
FROZEN = Path("eval/configs/thresholds.json")
LEDGER = Path("eval/reports/test_ledger.json")
FIGURES = {
    "detection_recall": "Detection recall (drifted chains, flagged before harm)",
    "false_block_rate": "False-block rate (control chains)",
    "violation_prevention": "Violation prevention (labeled violations halted)",
    "attribution_accuracy": "Attribution accuracy (culprit hop = mutated hop)",
}


def host_capabilities() -> frozenset[str]:
    """What this host can run: an NLI model, a judge, an embedder."""
    have = set()
    if os.environ.get("CL_JUDGE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
        have.add("judge")
    if os.environ.get("CL_NLI_MODEL"):
        have.add("nli")
    if os.environ.get("CL_EMBED_MODEL"):
        have.add("embedder")
    return frozenset(have)


class SplitLocked(RuntimeError):
    pass


def check_test_lock(configs: Sequence[EvalConfig], frozen: Path, ledger: Path) -> dict[str, Any]:
    if not frozen.exists():
        raise SplitLocked(
            f"the test split is locked: {frozen} does not exist. Thresholds must be tuned on "
            "val and frozen first (§14.4), after the human validation labels exist (ADR 0008)."
        )
    thresholds = json.loads(frozen.read_text("utf-8"))
    if thresholds.get("status") != "frozen":
        raise SplitLocked(f"{frozen} is not frozen (status={thresholds.get('status')!r})")
    used = {e["config"] for e in _ledger(ledger)}
    again = [c.id for c in configs if c.id in used]
    if again:
        raise SplitLocked(f"test was already run for {again}: once per configuration")
    return thresholds


def _ledger(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text("utf-8")) if path.exists() else []


def load_thresholds(split: str) -> dict[str, Any]:
    path = FROZEN if split == "test" else PROVISIONAL
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {"status": "defaults", "dwell_threshold_s": DEFAULT_DWELL_S}


async def evaluate(
    chains: Sequence[ChainRec],
    configs: Sequence[EvalConfig],
    *,
    dwell_s: float,
    have: frozenset[str],
    resamples: int,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for config in configs:
        meta = {
            "label": config.label,
            "description": config.description,
            "analysis_only": config.analysis_only,
        }
        missing = config.missing(have)
        if missing:
            results[config.id] = {**meta, "run": False, "reason": f"needs {', '.join(missing)}"}
            continue
        outs = [outcome(await run_chain(c, config, dwell_s=dwell_s)) for c in chains]
        results[config.id] = {
            **meta,
            "run": True,
            **summary(outs),
            "ci95": bootstrap(outs, resamples=resamples),
        }
    return results


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def _ci(ci: Any) -> str:
    return "" if not ci else f" [{100 * ci[0]:.1f}, {100 * ci[1]:.1f}]"


def render_markdown(report: dict[str, Any]) -> str:
    th = report["thresholds"]
    lines = [
        f"# Evaluation on driftchain-bench `{report['split']}` (Phase 8)",
        "",
        f"Benchmark `{report['benchmark']}`: {report['chains']} chains "
        f"({report['drifted']} drifted). Thresholds: **{th.get('status')}** "
        f"(dwell {th.get('dwell_threshold_s'):.0f} s, θ_low {th.get('theta_low', 0.35)}, "
        f"θ_high {th.get('theta_high', 0.8)}, δ_min {th.get('delta_min', 0.15)}). "
        f"95% bootstrap intervals over chains ({report['resamples']} resamples, seed 7). "
        f"Generated {report['generated_at']}.",
        "",
        "Definitions: `eval/metrics.py` and ADR 0010. `R` is ConstraintLens with the rules "
        "tiers only (C1 without Tier 1c). `ORACLE` is an analysis upper bound, not a system.",
        "",
        "## Headline (§14.3)",
        "",
        "| Config | Detection recall | Violation prevention | False-block rate | Precision "
        "| F1 | Lag (hops) | Attribution | Composition recall |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for cid, r in report["results"].items():
        name = f"`{cid}` {r['label']}"
        if not r["run"]:
            lines.append(f"| {name} | not run: {r['reason']} | | | | | | | |")
            continue
        h, ci = r["headline"], r["ci95"]
        lag = h["mean_detection_lag"]
        lines.append(
            f"| {name} | {_pct(h['detection_recall'])}{_ci(ci.get('detection_recall'))} "
            f"| {_pct(h['violation_prevention'])}{_ci(ci.get('violation_prevention'))} "
            f"| {_pct(h['false_block_rate'])}{_ci(ci.get('false_block_rate'))} "
            f"| {_pct(h['precision'])} | {_pct(h['f1'])} "
            f"| {'–' if lag is None else f'{lag:.2f}'} "
            f"| {_pct(h['attribution_accuracy'])} | {_pct(h['composition_recall'])} |"
        )
    ran = {k: v for k, v in report["results"].items() if v["run"]}
    ops = sorted({op for r in ran.values() for op in r["by_operator"]})
    lines += [
        "",
        "## Detection recall by operator (mandatory per-operator breakdown, §14.3)",
        "",
        "| Operator | " + " | ".join(f"`{k}`" for k in ran) + " |",
        "|---|" + "---|" * len(ran),
    ]
    for op in ops:
        cells = []
        for r in ran.values():
            row = r["by_operator"].get(op, {})
            value = row.get("false_block_rate") if op == "NONE" else row.get("detection_recall")
            cells.append(_pct(value))
        label = "NONE (false-block rate)" if op == "NONE" else op
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Async slices (RQ5): detection recall / false-block rate",
        "",
        "| Slice | " + " | ".join(f"`{k}`" for k in ran) + " |",
        "|---|" + "---|" * len(ran),
    ]
    for dim in ("dwell", "reorder", "redelivery", "fan_in"):
        values = sorted({v for r in ran.values() for v in r["async"][dim]})
        for value in values:
            cells = []
            for r in ran.values():
                row = r["async"][dim].get(value, {})
                cells.append(
                    f"{_pct(row.get('detection_recall'))} / {_pct(row.get('false_block_rate'))}"
                )
            lines.append(f"| {dim} = {value} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Redelivered checkpoints that got a different verdict than the first delivery: "
        + ", ".join(f"`{k}` {r['redelivery_conflicts']}" for k, r in ran.items())
        + ".",
        "",
        "## Overhead (RQ7) and degraded behavior",
        "",
        "| Config | checks | degraded checks | end-to-end p50 / p95 / p99 (ms) |",
        "|---|---|---|---|",
    ]
    for cid, r in ran.items():
        e2e = r["latency_ms"].get("end_to_end")
        lat = "–" if not e2e else f"{e2e['p50']:.3f} / {e2e['p95']:.3f} / {e2e['p99']:.3f}"
        lines.append(f"| `{cid}` | {r['checks']} | {_pct(r['degraded_check_rate'])} | {lat} |")
    lines += [
        "",
        "Latency is checker time per checkpoint on this host (in-process store); the "
        "service end-to-end budget is measured by `make latency`. Judge usage, cache hit "
        "rate and cost per 1,000 checks need Tier 2, which did not run.",
        "",
        "## Not yet run",
        "",
        "- Configurations needing an NLI model or a judge (listed above).",
        "- The `test` split: locked until thresholds are frozen (human validation labels "
        "pending, ADR 0008).",
        "- Live-pipeline trials with real LLM agents (§14.4 step 6): need an agent model key.",
        "",
    ]
    return "\n".join(lines)


def write_figures(report: dict[str, Any], out: Path) -> list[str]:
    figdir = out / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    written = []
    for metric, title in FIGURES.items():
        rows = []
        for cid, r in report["results"].items():
            if not r["run"]:
                rows.append((f"{cid}  {r['label']}", None, None))
                continue
            rows.append((f"{cid}  {r['label']}", r["headline"][metric], r["ci95"].get(metric)))
        sub = (
            f"driftchain-bench {report['split']}, {report['chains']} chains; "
            "whiskers: 95% bootstrap CI"
        )
        name = f"{report['split']}_{metric}.svg"
        (figdir / name).write_text(bar_chart(title, sub, rows), "utf-8")
        written.append(name)
    return written


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--split", default="val", choices=["dev", "val", "test"])
    parser.add_argument("--configs", nargs="*", default=None, help="config ids (default: all)")
    parser.add_argument("--limit", type=int, default=0, help="first N chains (debugging)")
    parser.add_argument("--resamples", type=int, default=1000)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    configs = [get(c) for c in args.configs] if args.configs else list(CONFIGS)
    if args.split == "test":
        try:
            check_test_lock(configs, FROZEN, LEDGER)
        except SplitLocked as exc:
            parser.exit(2, f"error: {exc}\n")
    if not args.data.exists():
        parser.error(f"{args.data} not found; run `make bench` first")
    chains = [c for c in read_jsonl(args.data) if c.split == args.split]
    if args.limit:
        chains = chains[: args.limit]
    thresholds = load_thresholds(args.split)
    have = host_capabilities()
    results = asyncio.run(
        evaluate(
            chains, configs, dwell_s=float(thresholds["dwell_threshold_s"]), have=have,
            resamples=args.resamples,
        )
    )  # fmt: skip
    report = {
        "benchmark": args.data.name,
        "split": args.split,
        "chains": len(chains),
        "drifted": sum(c.label.drift for c in chains),
        "thresholds": thresholds,
        "host_capabilities": sorted(have),
        "resamples": args.resamples,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "results": results,
    }
    md = render_markdown(report)
    print(md)
    if args.out and not args.limit:
        args.out.mkdir(parents=True, exist_ok=True)
        stem = f"eval_{args.split}_v1"
        (args.out / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n", "utf-8")
        (args.out / f"{stem}.md").write_text(md, "utf-8")
        write_figures(report, args.out)
        if args.split == "test":
            ledger = _ledger(LEDGER)
            ledger += [
                {"config": c.id, "run_at": report["generated_at"]}
                for c in configs
                if results[c.id]["run"]
            ]
            LEDGER.write_text(json.dumps(ledger, indent=2) + "\n", "utf-8")


if __name__ == "__main__":
    main()
