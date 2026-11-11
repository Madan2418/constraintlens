"""Evaluation configurations (ARCHITECTURE.md §14.2, ADR 0010).

Each configuration says which tiers run, at which checkpoints, and whether the async
guard and composition are on. ``needs`` lists the models a configuration cannot run
without; ``available`` filters on what this host has. A configuration that cannot run
is reported as *not run*, never substituted silently.

Two configurations are additions, both labeled as such in every report:

* ``R``: ConstraintLens with the rules tiers only (T0, T1a, T1b, async guard,
  composition). It is C1 without Tier 1c, the system that runs when no NLI model is
  loaded (ADR 0007). The C3 ablations that need no model are taken from it.
* ``ORACLE``: Tiers 0 and 1a plus the benchmark's own reference features as an exact
  text detector. It is an analysis upper bound and is never a system result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Checkpoints = Literal["all", "first_hop", "tool_call", "none"]
Need = Literal["nli", "judge", "embedder"]


@dataclass(frozen=True)
class EvalConfig:
    id: str
    label: str
    description: str
    tiers: tuple[str, ...] = ()
    checkpoints: Checkpoints = "all"
    guard: bool = True
    composition: bool = True
    needs: tuple[Need, ...] = ()
    analysis_only: bool = False  # upper bounds and diagnostics, never a system result

    def missing(self, have: frozenset[str]) -> list[str]:
        return [n for n in self.needs if n not in have]


RULES = ("T0", "T1a", "T1b")

CONFIGS: tuple[EvalConfig, ...] = (
    EvalConfig("B0", "No check", "Pipeline runs unprotected.", checkpoints="none"),
    EvalConfig(
        "B1",
        "Presence-only",
        "Constraint object present and hash intact (Tier 0 integrity) at every checkpoint.",
        tiers=("T0",),
        guard=False,
    ),
    EvalConfig(
        "B2",
        "One-shot gate",
        "A single text check at the first hop only (CONSUME of T_0 and PUBLISH of T_1). "
        "Without an NLI model the check is Tier 1b, the strongest text tier available.",
        tiers=("T0", "T1b"),
        checkpoints="first_hop",
        guard=False,
    ),
    EvalConfig(
        "B3",
        "Tool-arg check only",
        "Tier 1a only, at the tool call.",
        tiers=("T1a",),
        checkpoints="tool_call",
        guard=False,
    ),
    EvalConfig(
        "R",
        "ConstraintLens, rules tiers",
        "T0, T1a, T1b with the async guard and composition: C1 without Tier 1c.",
        tiers=RULES,
    ),
    EvalConfig(
        "C1",
        "ConstraintLens, no Tier 2",
        "Tiers 0 to 1c.",
        tiers=(*RULES, "T1c"),
        needs=("nli",),
    ),
    EvalConfig(
        "C2",
        "ConstraintLens, full",
        "Tiers 0 to 2 with cache.",
        tiers=(*RULES, "T1c", "T2"),
        needs=("nli", "judge"),
    ),
    EvalConfig(
        "C3-T1b",
        "Ablation: no lexical tier",
        "R without Tier 1b.",
        tiers=("T0", "T1a"),
    ),
    EvalConfig(
        "C3-guard",
        "Ablation: no async guard",
        "R without staleness, ordering and gap checks.",
        tiers=RULES,
        guard=False,
    ),
    EvalConfig(
        "C3-comp",
        "Ablation: no composition",
        "R tracking only the user's constraint; org policies are not registered.",
        tiers=RULES,
        composition=False,
    ),
    EvalConfig(
        "C3-T1c",
        "Ablation: no NLI",
        "C2 without Tier 1c.",
        tiers=(*RULES, "T2"),
        needs=("judge",),
    ),
    EvalConfig(
        "C3-T2",
        "Ablation: no judge",
        "C2 without Tier 2 (= C1).",
        tiers=(*RULES, "T1c"),
        needs=("nli",),
    ),
    EvalConfig(
        "EMB",
        "Embedding similarity only",
        "Cosine similarity of T_k to the canonical text, to show its numeric and negation "
        "blind spots (§7.4).",
        tiers=("EMB",),
        needs=("embedder",),
        guard=False,
    ),
    EvalConfig(
        "ORACLE",
        "Oracle text detector (analysis)",
        "T0, T1a and the benchmark's reference features: an upper bound, not a system.",
        tiers=("T0", "T1a", "ORACLE"),
        analysis_only=True,
    ),
)

BY_ID = {c.id: c for c in CONFIGS}


def get(config_id: str) -> EvalConfig:
    try:
        return BY_ID[config_id]
    except KeyError:
        raise ValueError(f"unknown configuration {config_id!r}; known: {sorted(BY_ID)}") from None
