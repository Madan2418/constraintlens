"""Phase 8 evaluation harness: configurations, metrics, bootstrap, test lock, figures.

The release JSONL is gitignored, so chains are generated in-process.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree

import pytest

from driftchain_bench.generator import MAX_AGE_S, generate
from eval.configs import CONFIGS, get
from eval.figures import bar_chart
from eval.metrics import Outcome, headline, outcome, summary
from eval.run import SplitLocked, check_test_lock, evaluate, render_markdown, write_figures
from eval.runner import run_chain
from eval.stats import bootstrap
from eval.tune import choose

if TYPE_CHECKING:
    from pathlib import Path

CHAINS = generate(seed=11, size=64)


def by_op(op: str) -> list[Any]:
    return [c for c in CHAINS if c.label.mutation_op == op]


# --------------------------------------------------------------------------- #
# Configurations
# --------------------------------------------------------------------------- #


def test_registry_covers_section_14_2() -> None:
    ids = {c.id for c in CONFIGS}
    assert {"B0", "B1", "B2", "B3", "C1", "C2", "EMB"} <= ids
    assert {"C3-T1b", "C3-T1c", "C3-T2", "C3-guard", "C3-comp"} <= ids
    assert get("C2").missing(frozenset()) == ["nli", "judge"]
    assert get("C2").missing(frozenset({"nli", "judge"})) == []
    assert [c.id for c in CONFIGS if c.analysis_only] == ["ORACLE"]
    with pytest.raises(ValueError, match="unknown configuration"):
        get("C9")


async def test_b0_submits_nothing_and_b3_only_tool_calls() -> None:
    chain = CHAINS[0]
    assert (await run_chain(chain, get("B0"))).checks == []
    b3 = await run_chain(chain, get("B3"))
    assert b3.checks
    assert {c.checkpoint for c in b3.checks} == {"TOOL_CALL"}
    b2 = await run_chain(chain, get("B2"))
    assert {c.hop for c in b2.checks} == {1}


async def test_no_composition_registers_only_the_user_constraint() -> None:
    chain = next(c for c in CHAINS if len(c.constraints) == 2)
    run = await run_chain(chain, get("C3-comp"))
    assert set(run.ids) == {c.id for c in chain.constraints if c.scope == "user"}


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


async def test_oracle_detects_every_textual_drift_and_never_false_blocks() -> None:
    for chain in CHAINS:
        o = outcome(await run_chain(chain, get("ORACLE"), dwell_s=float(MAX_AGE_S)))
        if not chain.label.drift:
            assert o.false_block is False
            assert o.detected is None
        elif chain.label.mutation_op != "STALE_CONTEXT":
            assert o.detected, chain.chain_id
            assert o.lag == 0


async def test_unprotected_baseline_prevents_nothing() -> None:
    outs = [outcome(await run_chain(c, get("B0"))) for c in CHAINS]
    h = headline(outs)
    assert h["detection_recall"] == 0.0
    assert h["violation_prevention"] == 0.0
    assert h["false_block_rate"] == 0.0
    assert h["precision"] is None


async def test_tool_gate_blocks_amount_violations() -> None:
    outs = [outcome(await run_chain(c, get("B3"))) for c in by_op("NUMERIC_LOOSEN")]
    violations = [o for o in outs if o.violation]
    assert violations
    assert all(o.prevented for o in violations)


async def test_low_risk_stale_warning_counts_as_a_flag() -> None:
    stale = by_op("STALE_CONTEXT")
    assert stale
    outs = [outcome(await run_chain(c, get("R"), dwell_s=float(MAX_AGE_S))) for c in stale]
    assert all(o.detected for o in outs)


async def test_composition_recall_needs_composition() -> None:
    drop = by_op("CONSTRAINT_DROP_ONE")
    assert drop
    with_comp = [outcome(await run_chain(c, get("R"))) for c in drop]
    without = [outcome(await run_chain(c, get("C3-comp"))) for c in drop]
    assert headline(with_comp)["composition_recall"] == 1.0
    assert headline(without)["composition_recall"] == 0.0


async def test_redelivery_never_yields_a_conflicting_verdict() -> None:
    redelivered = [c for c in CHAINS if c.profile.redelivery]
    assert redelivered
    outs = [outcome(await run_chain(c, get("R"))) for c in redelivered]
    assert sum(o.redelivery_conflicts for o in outs) == 0
    s = summary(outs)
    assert s["latency_ms"]["end_to_end"]["n"] > 0


def _o(drift: bool, detected: bool | None, flagged: bool, fb: bool | None) -> Outcome:
    return Outcome(
        chain_id="x", drift=drift, op="NONE" if not drift else "NUMERIC_LOOSEN",
        topology="linear", profile={}, flagged=flagged, detected=detected, lag=0 if detected
        else None, violation=False, prevented=None, false_block=fb, attribution_hop=None,
        attribution_cause=None, composition_named=None, degraded_checks=0, checks=1,
        redelivery_conflicts=0,
    )  # fmt: skip


def test_headline_arithmetic() -> None:
    outs = [_o(True, True, True, None)] * 3 + [_o(True, False, False, None)]
    outs += [_o(False, None, True, True), _o(False, None, False, False)]
    h = headline(outs)
    assert h["detection_recall"] == 0.75
    assert h["precision"] == 0.75  # 3 TP, 1 flagged control
    assert h["f1"] == 0.75
    assert h["false_block_rate"] == 0.5


def test_bootstrap_is_seeded_and_brackets_the_estimate() -> None:
    outs = [_o(True, i % 4 != 0, True, None) for i in range(40)]
    outs += [_o(False, None, i % 5 == 0, i % 5 == 0) for i in range(40)]
    a = bootstrap(outs, resamples=200)
    assert a == bootstrap(outs, resamples=200)
    lo, hi = a["detection_recall"] or (0.0, 0.0)
    assert lo <= headline(outs)["detection_recall"] <= hi  # type: ignore[operator]
    assert a["mean_detection_lag"] is not None
    assert bootstrap([]) == {}


# --------------------------------------------------------------------------- #
# Tuning, the test lock, reports
# --------------------------------------------------------------------------- #


def test_tuning_objective() -> None:
    rows = [
        {"dwell_threshold_s": 300, "detection_recall": 0.95, "false_block_rate": 0.5},
        {"dwell_threshold_s": 3600, "detection_recall": 0.93, "false_block_rate": 0.04},
        {"dwell_threshold_s": 7200, "detection_recall": 0.93, "false_block_rate": 0.04},
    ]
    best, met = choose(rows)
    assert (best["dwell_threshold_s"], met) == (3600, True)  # tie -> smaller threshold
    best, met = choose([{**rows[0]}, {**rows[0], "dwell_threshold_s": 600,
                                      "false_block_rate": 0.2}])  # fmt: skip
    assert (best["dwell_threshold_s"], met) == (600, False)  # no value meets the bound


def test_test_split_is_locked(tmp_path: Path) -> None:
    frozen, ledger = tmp_path / "thresholds.json", tmp_path / "ledger.json"
    with pytest.raises(SplitLocked, match="does not exist"):
        check_test_lock([get("R")], frozen, ledger)
    frozen.write_text(json.dumps({"status": "provisional"}))
    with pytest.raises(SplitLocked, match="not frozen"):
        check_test_lock([get("R")], frozen, ledger)
    frozen.write_text(json.dumps({"status": "frozen", "dwell_threshold_s": 3600}))
    assert check_test_lock([get("R")], frozen, ledger)["status"] == "frozen"
    ledger.write_text(json.dumps([{"config": "R", "run_at": "t"}]))
    with pytest.raises(SplitLocked, match="once per configuration"):
        check_test_lock([get("R")], frozen, ledger)
    check_test_lock([get("B3")], frozen, ledger)


async def test_report_and_figures(tmp_path: Path) -> None:
    results = await evaluate(
        CHAINS[:16], [get("B0"), get("R"), get("C2")], dwell_s=300.0, have=frozenset(),
        resamples=50,
    )  # fmt: skip
    assert results["C2"] == {
        "label": "ConstraintLens, full", "description": "Tiers 0 to 2 with cache.",
        "analysis_only": False, "run": False, "reason": "needs nli, judge",
    }  # fmt: skip
    report = {
        "benchmark": "generated", "split": "dev", "chains": 16, "drifted": 8,
        "thresholds": {"status": "defaults", "dwell_threshold_s": 300.0}, "resamples": 50,
        "generated_at": "now", "results": results,
    }  # fmt: skip
    md = render_markdown(report)
    assert "not run: needs nli, judge" in md
    assert "Async slices" in md
    names = write_figures(report, tmp_path)
    assert len(names) == 4
    for name in names:
        ElementTree.fromstring((tmp_path / "figures" / name).read_text("utf-8"))  # valid XML


def test_bar_chart_marks() -> None:
    svg = bar_chart("t", "s", [("a", 0.5, (0.4, 0.6)), ("b", None, None), ("c", 0.0, None)])
    root = ElementTree.fromstring(svg)
    assert root.tag.endswith("svg")
    assert "not run" in svg
    assert "50.0%" in svg
    assert "95% CI 40.0% to 60.0%" in svg
