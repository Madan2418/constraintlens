"""Extraction accuracy on the hand-written gold set (ARCHITECTURE.md §23 Phase 1 exit criterion).

Usage::

    uv run python -m eval.extraction_accuracy            # rules only
    uv run python -m eval.extraction_accuracy --out eval/reports

Metrics
-------
* **instruction exact match**: the predicted constraint set equals the gold set
  (including "no constraint" instructions).
* **constraint precision / recall / F1**: a predicted constraint counts only if every
  field matches a gold constraint (action, resource, subject, max, min, unit,
  authority, expiry).
* **field accuracy**: for gold constraints aligned to a prediction with the same
  action, the share whose individual field is right.
* **false-constraint rate**: share of no-constraint instructions where anything was
  extracted.

Reported separately for the ``dev`` and ``holdout`` halves of the set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from constraintlens.domain.canonical import canonical_decimal
from constraintlens.domain.extraction import ExtractionContext, LLMMode, extract
from constraintlens.domain.subject import SubjectScope

if TYPE_CHECKING:
    from constraintlens.domain.models import ConstraintSpec
    from constraintlens.domain.ports import Judge

GOLD = Path(__file__).parent / "extraction" / "gold_v1.jsonl"
ISSUED_AT = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
FIELDS = ("action", "resource", "subject", "max", "min", "unit", "authority", "expiry")

Key = tuple[Any, ...]


def _dec(v: Any) -> str | None:
    return None if v is None else canonical_decimal(Decimal(str(v)))


def gold_key(c: dict[str, Any]) -> Key:
    return (
        c["action"],
        c["resource"],
        SubjectScope.parse(c["subject"]).format(),
        _dec(c["max"]),
        _dec(c["min"]),
        c["unit"],
        c["authority"],
        c["expiry"],
    )


def spec_key(s: ConstraintSpec) -> Key:
    lim = s.limit
    return (
        s.action,
        s.resource,
        SubjectScope.parse(s.subject).format(),
        _dec(lim.max_value) if lim else None,
        _dec(lim.min_value) if lim else None,
        lim.unit if lim else None,
        s.authority,
        s.expiry.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if s.expiry else None,
    )


@dataclass
class Tally:
    n: int = 0
    exact: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    empty_n: int = 0
    empty_fp: int = 0
    field_right: Counter[str] = field(default_factory=Counter)
    field_total: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        p = self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0
        r = self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0
        return {
            "instructions": self.n,
            "instruction_exact_match": round(self.exact / self.n, 4) if self.n else None,
            "constraint_precision": round(p, 4),
            "constraint_recall": round(r, 4),
            "constraint_f1": round(2 * p * r / (p + r), 4) if p + r else 0.0,
            "false_constraint_rate": (
                round(self.empty_fp / self.empty_n, 4) if self.empty_n else None
            ),
            "field_accuracy": {
                f: round(self.field_right[f] / self.field_total, 4) if self.field_total else None
                for f in FIELDS
            },
        }


def score_one(tally: Tally, record: dict[str, Any], predicted: list[Key]) -> None:
    gold = [gold_key(c) for c in record["constraints"]]
    tally.n += 1
    if not gold:
        tally.empty_n += 1
        tally.empty_fp += bool(predicted)
    if Counter(gold) == Counter(predicted):
        tally.exact += 1
    else:
        tally.errors.append(
            {"id": record["id"], "text": record["text"], "gold": gold, "predicted": predicted}
        )
    remaining = list(predicted)
    for g in gold:
        if g in remaining:
            remaining.remove(g)
            tally.tp += 1
        else:
            tally.fn += 1
    tally.fp += len(remaining)
    # field accuracy: align each gold constraint with an unused prediction of the same action
    pool = list(predicted)
    for g in gold:
        tally.field_total += 1
        match = next((p for p in pool if p == g), None) or next(
            (p for p in pool if p[0] == g[0]), None
        )
        if match is None:
            continue
        pool.remove(match)
        for i, f in enumerate(FIELDS):
            tally.field_right[f] += match[i] == g[i]


def load_gold(path: Path = GOLD) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


async def evaluate(
    records: list[dict[str, Any]],
    *,
    judge: Judge | None = None,
    llm_mode: LLMMode = "off",
) -> dict[str, Tally]:
    tallies: dict[str, Tally] = {"all": Tally()}
    ctx = ExtractionContext(owner="user:eval", issued_at=ISSUED_AT)
    for rec in records:
        result = await extract(rec["text"], ctx, judge=judge, llm_mode=llm_mode)
        predicted = [spec_key(s) for s in result.specs]
        for name in ("all", rec["split"]):
            score_one(tallies.setdefault(name, Tally()), rec, predicted)
    return tallies


def render_markdown(tallies: dict[str, Tally], *, label: str, gold: Path = GOLD) -> str:
    sums = {k: t.summary() for k, t in tallies.items()}
    rows = [
        "| Metric | " + " | ".join(k.capitalize() for k in sums) + " |",
        "|---|" + "---|" * len(sums),
    ]
    for metric in (
        "instructions",
        "instruction_exact_match",
        "constraint_precision",
        "constraint_recall",
        "constraint_f1",
        "false_constraint_rate",
    ):
        rows.append(f"| {metric} | " + " | ".join(str(sums[k][metric]) for k in sums) + " |")
    for f in FIELDS:
        rows.append(
            f"| field: {f} | " + " | ".join(str(sums[k]["field_accuracy"][f]) for k in sums) + " |"
        )
    errors = tallies["all"].errors
    lines = [
        f"# Extraction accuracy: {label}",
        "",
        f"Gold set: `eval/extraction/{gold.name}` ({tallies['all'].n} instructions).",
        "",
        *rows,
        "",
        f"## Mismatches ({len(errors)})",
        "",
    ]
    for e in errors:
        lines += [
            f"- **{e['id']}** {e['text']}",
            f"  - gold: `{e['gold']}`",
            f"  - got:  `{e['predicted']}`",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="write report files here")
    parser.add_argument("--gold", type=Path, default=GOLD, help="labelled JSONL set")
    args = parser.parse_args(argv)
    tallies = asyncio.run(evaluate(load_gold(args.gold)))
    label = "rule-based extractor (llm_mode=off)"
    report = render_markdown(tallies, label=label, gold=args.gold)
    print(report)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        stem = f"extraction_rules_{args.gold.stem}"
        (args.out / f"{stem}.md").write_text(report, encoding="utf-8")
        (args.out / f"{stem}.json").write_text(
            json.dumps({k: t.summary() for k, t in tallies.items()}, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
