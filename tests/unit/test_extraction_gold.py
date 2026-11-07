"""Phase 1 exit criterion: extraction accuracy measured on 100 hand-written instructions.

These are regression floors, not targets. The gold set and the rules share an author,
so the gold number is in-sample. The probe set was written after the rules were frozen
and is never used for tuning (see eval/reports/).
"""

from __future__ import annotations

from pathlib import Path

from eval.extraction_accuracy import evaluate, load_gold

GOLD_DIR = Path(__file__).resolve().parents[2] / "eval" / "extraction"


async def test_gold_set_shape() -> None:
    records = load_gold(GOLD_DIR / "gold_v1.jsonl")
    assert len(records) == 100
    assert len({r["id"] for r in records}) == 100
    assert {r["split"] for r in records} == {"dev", "holdout"}


async def test_gold_accuracy_floor() -> None:
    summary = (await evaluate(load_gold(GOLD_DIR / "gold_v1.jsonl")))["all"].summary()
    assert summary["instruction_exact_match"] >= 0.98
    assert summary["false_constraint_rate"] == 0.0


async def test_probe_accuracy_floor() -> None:
    summary = (await evaluate(load_gold(GOLD_DIR / "probe_v1.jsonl")))["all"].summary()
    assert summary["instruction_exact_match"] >= 0.85
    assert summary["constraint_precision"] == 1.0
