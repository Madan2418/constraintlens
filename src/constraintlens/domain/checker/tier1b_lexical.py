"""Tier 1b: lexical constraint check (ARCHITECTURE.md §7.4).

Sub-millisecond and deterministic. It extracts amounts, units, approval qualifiers and
loosening phrases from the task text T_k and compares them with each registered
constraint. It reports only what the words show:

* **limit loosened**: a phrase turns the bound into a permission or removes it
  ("may exceed $100", "no limit", "in full", "ignore previous limits");
* **limit raised**: T_k states amounts in the constraint's unit, none of them is the
  bound, and one exceeds it ("$1,000" where the constraint says $100). A stricter
  stated cap (e.g. the effective $50 under a $100 user limit) is compatible;
* **limit missing**: no amount in the unit and no generic reference to the bound
  ("within the limit", "within budget");
* **approval dropped / negated**: the constraint needs approval and T_k no longer
  mentions it, or says it is not needed.

Any finding is terminal (``TEXTUAL_DRIFT``, §7.4 table). When the words are compatible
or merely unspecific, the tier passes and the NLI tier decides. Tier 1b never claims
entailment: it produces no scores and does not count as re-verification of a stale
constraint (§9.3), although a finding on one does count as failed re-verification.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from constraintlens.domain.checker.pipeline import Finding, TierOutcome
from constraintlens.domain.extraction import find_quantities, mentions_approval, negates_approval
from constraintlens.domain.models import Failure, TierId

if TYPE_CHECKING:
    from constraintlens.domain.checker.pipeline import CheckContext
    from constraintlens.domain.models import Constraint

TEXT_CHECKPOINTS = frozenset({"PUBLISH", "CONSUME", "TOOL_CALL"})

_LOOSEN = re.compile(
    r"\b(?:may|can|could|is\s+allowed\s+to|allowed\s+to|free\s+to)\s+(?:go\s+)?"
    r"(?:exceed|go\s+over|go\s+above|surpass)\b"
    r"|\bno\s+(?:spending\s+|upper\s+)?(?:limit|cap|ceiling)\b"
    r"|\bwithout\s+(?:a\s+|any\s+)?(?:limit|cap|ceiling)\b"
    r"|\buncapped\b|\bunlimited\b|\bin\s+full\b"
    r"|\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|the|earlier)?\s*(?:limits?|caps?|constraints?)\b"
    r"|\bregardless\s+of\s+(?:the\s+)?(?:limit|amount|cost|price|budget)\b",
    re.IGNORECASE,
)
_GENERIC_REF = re.compile(
    r"\bwithin\s+(?:the\s+|its\s+|their\s+|our\s+)?"
    r"(?:approved\s+|agreed\s+|stated\s+|usual\s+|original\s+|existing\s+|same\s+)?"
    r"(?:limit|cap|budget|bounds?|allowance)\b"
    r"|\b(?:as|where)\s+(?:authori[sz]ed|instructed|approved)\b"
    r"|\b(?:the|that|this)\s+(?:same\s+)?(?:limit|cap|budget)\b",
    re.IGNORECASE,
)


def check_constraint(constraint: Constraint, text: str) -> list[str]:
    """Reasons ``text`` shows lexical drift from ``constraint`` (empty: no evidence)."""
    reasons: list[str] = []
    limit = constraint.limit
    if limit is not None and limit.max_value is not None:
        bound, unit = limit.max_value, limit.unit
        stated = [v for v, u in find_quantities(text) if u == unit]
        loosen = _LOOSEN.search(text)
        if loosen:
            reasons.append(f"limit loosened: {loosen.group(0)!r}")
        elif bound in stated:
            pass
        elif any(v > bound for v in stated):
            shown = ", ".join(f"{v:,} {unit}" for v in stated)
            reasons.append(f"limit {bound:,} {unit} not stated; text names {shown}")
        elif not stated and not _GENERIC_REF.search(text):
            reasons.append(f"limit {bound:,} {unit} missing from the task text")
    if constraint.authority != "none":
        if negates_approval(text):
            reasons.append("approval requirement negated")
        elif not mentions_approval(text):
            reasons.append("approval requirement dropped")
    return reasons


class LexicalTier:
    name: TierId = "T1b"

    def applies(self, ctx: CheckContext) -> bool:
        return (
            ctx.request.checkpoint in TEXT_CHECKPOINTS
            and bool(ctx.registered)
            and bool(ctx.request.envelope.payload_text.strip())
        )

    async def run(self, ctx: CheckContext) -> TierOutcome:
        text = ctx.request.envelope.payload_text
        findings = [
            Finding(Failure(kind="TEXTUAL_DRIFT", constraint_id=c.constraint_id, detail=r))
            for c in ctx.registered
            for r in check_constraint(c, text)
        ]
        return TierOutcome(findings=findings, terminal=bool(findings))
