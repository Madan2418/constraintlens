"""Hand validation: blind sample, labeling sheet, agreement statistics (ARCHITECTURE.md §13.6).

Workflow (the labeling guide is ``validation/GUIDE.md``):

1. ``sample``: draw a stratified random sample of at least 300 chains. Every scenario x
   operator cell is covered, and about half are controls. Write it **blind**: texts,
   actions and timing, but no operator, no mutated hop, no split. ``key.json`` maps
   sample ids to chain ids and holds no labels.
2. A person labels each chain in ``labels_template.csv`` (drift yes/no, culprit hop,
   violation yes/no) without looking at the dataset.
3. ``agreement``: compare the hand labels with the programmatic ones. It reports raw
   agreement and Cohen's kappa for drift and violation, culprit-hop accuracy, a
   per-operator breakdown, and every disagreement. With a second annotator's file it
   also reports inter-annotator kappa.
4. Each disagreement is inspected. It either fixes the generator (a new version) or is
   excluded and documented in ``exclusions.json`` with a reason.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import TYPE_CHECKING, Any

from driftchain_bench.operators import DRIFT_OPERATORS, OPERATORS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from driftchain_bench.record import ChainRec

VALIDATION_DIR = Path(__file__).parent / "validation"
CSV_FIELDS = ("sample_id", "drift", "culprit_hop", "violation", "violation_hop", "notes")


def sample_dir(version: str) -> Path:
    return VALIDATION_DIR / f"v{version}"


# --------------------------------------------------------------------------- sampling


def stratified_sample(chains: Sequence[ChainRec], n: int = 300, seed: int = 13) -> list[ChainRec]:
    """About half drifted (round-robin over scenario x operator), half controls."""
    if n < 1:
        raise ValueError("sample size must be positive")
    rng = Random(f"driftchain:validate:{seed}")
    cells: dict[tuple[str, str], list[ChainRec]] = defaultdict(list)
    for c in chains:
        cells[(c.scenario, c.label.mutation_op)].append(c)
    for members in cells.values():
        rng.shuffle(members)
    scenarios = sorted({c.scenario for c in chains})
    drift_cells = [(s, o) for s in scenarios for o in DRIFT_OPERATORS if cells.get((s, o))]
    control_cells = [(s, "NONE") for s in scenarios if cells.get((s, "NONE"))]

    def take(cell_list: list[tuple[str, str]], quota: int) -> list[ChainRec]:
        picked: list[ChainRec] = []
        cursor = dict.fromkeys(cell_list, 0)
        while len(picked) < quota and any(cursor[c] < len(cells[c]) for c in cell_list):
            for cell in cell_list:
                if len(picked) == quota:
                    break
                if cursor[cell] < len(cells[cell]):
                    picked.append(cells[cell][cursor[cell]])
                    cursor[cell] += 1
        return picked

    drifted = take(drift_cells, n - n // 2)
    controls = take(control_cells, n - len(drifted))
    out = drifted + controls
    rng.shuffle(out)
    return out


def blind_view(sample_id: str, chain: ChainRec) -> dict[str, Any]:
    """What an annotator sees: no operator, mutated hop, label, split or provenance."""
    return {
        "sample_id": sample_id,
        "scenario": chain.scenario,
        "instruction": chain.instruction,
        "constraints": [
            {
                "id": c.id,
                "text": c.canonical_text,
                "fresh_for_s": c.spec.max_age_seconds,
            }
            for c in chain.constraints
        ],
        "hops": [
            {
                "hop": h.hop,
                "agent": h.agent,
                "parents": h.parents,
                "text": h.text,
                "action": h.action.model_dump() if h.action else None,
                "queue_dwell_s": h.queue_dwell_s,
                "consumed_at_s": h.consumed_at_s,
                "arrival": h.arrival,
                "delivery": h.delivery,
            }
            for h in chain.hops
        ],
    }


def _duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def render_sheet(views: Sequence[dict[str, Any]], *, version: str) -> str:
    lines = [
        f"# driftchain-bench v{version}: blind validation sheet",
        "",
        "Label every chain in `labels_template.csv` before looking at anything else. "
        "The rules are in `../GUIDE.md`. Times are measured from the moment the "
        "constraints were issued.",
        "",
    ]
    for v in views:
        lines += [
            f"## {v['sample_id']} ({v['scenario']})",
            "",
            f"**User (T0):** {v['instruction']}",
            "",
        ]
        for c in v["constraints"]:
            fresh = (
                f" (valid for {_duration(c['fresh_for_s'])} after issue)"
                if c["fresh_for_s"]
                else ""
            )
            lines.append(f"- constraint {c['id']}: `{c['text']}`{fresh}")
        lines.append("")
        lines.append("| hop | agent | after | consumed at | arrival | text | action |")
        lines.append("|---|---|---|---|---|---|---|")
        for h in v["hops"]:
            parents = ", ".join(map(str, h["parents"])) or "user"
            when = _duration(h["consumed_at_s"])
            if h["delivery"] > 1:
                when += f" (delivery #{h['delivery']})"
            act = ""
            if h["action"]:
                args = ", ".join(f"{k}={val}" for k, val in h["action"]["args"].items())
                act = f"`{h['action']['tool']}({args})`"
            text = h["text"].replace("|", "\\|")
            cells = [str(h["hop"]), h["agent"], parents, when, str(h["arrival"]), text, act]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)


def write_sample(
    chains: Sequence[ChainRec], *, version: str, data_sha256: str, n: int = 300, seed: int = 13,
    out_dir: Path | None = None,
) -> Path:  # fmt: skip
    out = out_dir or sample_dir(version)
    out.mkdir(parents=True, exist_ok=True)
    picked = stratified_sample(chains, n=n, seed=seed)
    width = max(3, len(str(len(picked))))
    ids = [f"v{i + 1:0{width}d}" for i in range(len(picked))]
    views = [blind_view(sid, c) for sid, c in zip(ids, picked, strict=True)]
    (out / "blind.jsonl").write_text(
        "".join(json.dumps(v, ensure_ascii=False) + "\n" for v in views), encoding="utf-8",
        newline="\n",
    )  # fmt: skip
    (out / "sheet.md").write_text(render_sheet(views, version=version) + "\n", encoding="utf-8",
                                  newline="\n")  # fmt: skip
    with (out / "labels_template.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(CSV_FIELDS)
        for sid in ids:
            writer.writerow([sid, "", "", "", "", ""])
    key = {
        "generator_version": version,
        "data_sha256": data_sha256,
        "sample_seed": seed,
        "size": len(picked),
        "samples": {sid: c.chain_id for sid, c in zip(ids, picked, strict=True)},
    }
    (out / "key.json").write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8", newline="\n")
    exclusions = out / "exclusions.json"
    if not exclusions.exists():
        exclusions.write_text("[]\n", encoding="utf-8", newline="\n")
    return out


# --------------------------------------------------------------------------- agreement


@dataclass(frozen=True)
class HandLabel:
    drift: bool
    culprit_hop: int | None
    violation: bool
    violation_hop: int | None


def _yes_no(value: str, *, field_name: str, sid: str) -> bool:
    v = value.strip().lower()
    if v in {"y", "yes", "1", "true"}:
        return True
    if v in {"n", "no", "0", "false"}:
        return False
    raise ValueError(f"{sid}: {field_name} must be yes or no, got {value!r}")


def _hop(value: str) -> int | None:
    v = value.strip()
    return int(v) if v else None


def read_labels(path: Path) -> dict[str, HandLabel]:
    """Completed rows only; a row with an empty ``drift`` cell is not labeled yet."""
    out = {}
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            sid = row["sample_id"].strip()
            if not row.get("drift", "").strip():
                continue
            out[sid] = HandLabel(
                drift=_yes_no(row["drift"], field_name="drift", sid=sid),
                culprit_hop=_hop(row.get("culprit_hop", "")),
                violation=_yes_no(row.get("violation", ""), field_name="violation", sid=sid),
                violation_hop=_hop(row.get("violation_hop", "")),
            )
    return out


def cohen_kappa(pairs: Sequence[tuple[bool, bool]]) -> float | None:
    """Cohen's kappa for two binary raters; None when undefined (no pairs, or chance = 1)."""
    n = len(pairs)
    if n == 0:
        return None
    observed = sum(a == b for a, b in pairs) / n
    pa = sum(a for a, _ in pairs) / n
    pb = sum(b for _, b in pairs) / n
    chance = pa * pb + (1 - pa) * (1 - pb)
    if chance >= 1.0:
        return None
    return (observed - chance) / (1 - chance)


def agreement(
    chains_by_id: dict[str, ChainRec],
    key: dict[str, Any],
    labels: dict[str, HandLabel],
    second: dict[str, HandLabel] | None = None,
) -> dict[str, Any]:
    rows = []
    for sid, chain_id in key["samples"].items():
        if sid in labels:
            rows.append((sid, chains_by_id[chain_id], labels[sid]))
    drift_pairs = [(c.label.drift, h.drift) for _, c, h in rows]
    viol_pairs = [(c.label.violation_at_hop is not None, h.violation) for _, c, h in rows]
    both_drift = [(c, h) for _, c, h in rows if c.label.drift and h.drift]
    hop_hits = sum(c.label.mutated_hop == h.culprit_hop for c, h in both_drift)
    per_op: dict[str, dict[str, int]] = {}
    for op in OPERATORS:
        sub = [(c, h) for _, c, h in rows if c.label.mutation_op == op]
        per_op[op] = {"n": len(sub), "drift_agree": sum(c.label.drift == h.drift for c, h in sub)}
    disagreements = [
        {
            "sample_id": sid,
            "chain_id": c.chain_id,
            "operator": c.label.mutation_op,
            "programmatic": {
                "drift": c.label.drift,
                "culprit_hop": c.label.mutated_hop,
                "violation": c.label.violation_at_hop is not None,
            },
            "hand": {"drift": h.drift, "culprit_hop": h.culprit_hop, "violation": h.violation},
        }
        for sid, c, h in rows
        if c.label.drift != h.drift
        or (c.label.violation_at_hop is not None) != h.violation
        or (c.label.drift and h.drift and c.label.mutated_hop != h.culprit_hop)
    ]
    n = len(rows)
    report: dict[str, Any] = {
        "sample_size": len(key["samples"]),
        "labeled": n,
        "drift": {
            "agreement": (sum(a == b for a, b in drift_pairs) / n) if n else None,
            "kappa": cohen_kappa(drift_pairs),
        },
        "violation": {
            "agreement": (sum(a == b for a, b in viol_pairs) / n) if n else None,
            "kappa": cohen_kappa(viol_pairs),
        },
        "culprit_hop": {
            "both_say_drift": len(both_drift),
            "accuracy": (hop_hits / len(both_drift)) if both_drift else None,
        },
        "per_operator": per_op,
        "disagreements": disagreements,
    }
    if second is not None:
        shared = sorted(set(labels) & set(second))
        report["inter_annotator"] = {
            "labeled_by_both": len(shared),
            "drift_kappa": cohen_kappa([(labels[s].drift, second[s].drift) for s in shared]),
            "violation_kappa": cohen_kappa(
                [(labels[s].violation, second[s].violation) for s in shared]
            ),
        }
    return report


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _num(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def render_agreement(report: dict[str, Any], *, version: str) -> str:
    lines = [
        f"# driftchain-bench v{version}: hand-validation agreement",
        "",
        f"Labeled {report['labeled']} of {report['sample_size']} sampled chains.",
        "",
        "| measure | agreement | Cohen's kappa |",
        "|---|---|---|",
        f"| drift | {_pct(report['drift']['agreement'])} | {_num(report['drift']['kappa'])} |",
        f"| violation | {_pct(report['violation']['agreement'])} | "
        f"{_num(report['violation']['kappa'])} |",
        "",
        f"Culprit hop: {_pct(report['culprit_hop']['accuracy'])} exact, over "
        f"{report['culprit_hop']['both_say_drift']} chains both sides call drifted.",
        "",
    ]
    if "inter_annotator" in report:
        ia = report["inter_annotator"]
        lines += [
            f"Inter-annotator ({ia['labeled_by_both']} chains): drift kappa "
            f"{_num(ia['drift_kappa'])}, violation kappa {_num(ia['violation_kappa'])}.",
            "",
        ]
    lines += ["| operator | labeled | drift agreement |", "|---|---|---|"]
    for op, row in report["per_operator"].items():
        share = row["drift_agree"] / row["n"] if row["n"] else None
        lines.append(f"| {op} | {row['n']} | {_pct(share)} |")
    lines += ["", f"## Disagreements ({len(report['disagreements'])})", ""]
    for d in report["disagreements"]:
        lines.append(
            f"- {d['sample_id']} ({d['chain_id']}, {d['operator']}): programmatic "
            f"{d['programmatic']}, hand {d['hand']}"
        )
    return "\n".join(lines) + "\n"


def load_exclusions(version: str) -> dict[str, str]:
    """``{chain_id: reason}`` for chains excluded after validation (§13.6 item 2)."""
    path = sample_dir(version) / "exclusions.json"
    if not path.exists():
        return {}
    return {e["chain_id"]: e["reason"] for e in json.loads(path.read_text(encoding="utf-8"))}
