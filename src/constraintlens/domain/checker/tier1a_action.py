"""Tier 1a: structured comparison of the proposed action (ARCHITECTURE.md §7.4).

When A_k has a binding (tool → action, resource, amount, unit, subject), compare it
directly with every registered constraint on that ``(action, resource)``. This is
free, exact, and independent of any task text, so it is authoritative for the
action-violation failure (§7.4 rule 2) and immune to prompt injection (§21.1).

The combined outcome comes from the *effective* constraint (most-restrictive-wins,
§7.6). Each individual constraint the action breaks is named in its own failure.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from constraintlens.domain.canonical import canonical_decimal
from constraintlens.domain.checker.pipeline import Finding, TierOutcome
from constraintlens.domain.composition import compose
from constraintlens.domain.models import ActionBinding, Failure, TierId

if TYPE_CHECKING:
    from datetime import datetime

    from constraintlens.domain.checker.pipeline import CheckContext
    from constraintlens.domain.composition import EffectiveConstraint, Outcome
    from constraintlens.domain.models import Constraint, ProposedAction


class _Use:
    """The proposed action reduced to the dimensions constraints speak about."""

    def __init__(self, action: ProposedAction, binding: ActionBinding) -> None:
        args = action.args
        self.amount: Decimal | None = None
        self.unit: str | None = None
        self.subject: str | None = None
        self.problems: list[str] = []
        if binding.amount_arg is not None:
            self.amount = _decimal(args.get(binding.amount_arg))
            if self.amount is None:
                self.problems.append(f"argument {binding.amount_arg!r} is missing or not a number")
            self.unit = binding.unit or (
                str(args[binding.unit_arg]) if binding.unit_arg in args else None
            )
            if self.unit is None:
                self.problems.append(f"argument {binding.unit_arg!r} (unit) is missing")
        if binding.subject_arg is not None:
            raw = args.get(binding.subject_arg)
            if raw is None:
                self.problems.append(f"argument {binding.subject_arg!r} (subject) is missing")
            else:
                item = str(raw).lower()
                self.subject = f"{binding.subject_kind}:{item}" if binding.subject_kind else item

    def evaluate(self, eff: EffectiveConstraint, at: datetime) -> Outcome:
        return eff.evaluate(at=at, amount=self.amount, unit=self.unit, subject_item=self.subject)

    def describe(self) -> str:
        parts = []
        if self.amount is not None:
            parts.append(f"amount={canonical_decimal(self.amount)} {self.unit}")
        if self.subject is not None:
            parts.append(f"subject={self.subject}")
        return ", ".join(parts) or "no bound arguments"


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _reason(outcome: str, use: _Use, c: Constraint) -> str:
    if outcome == "needs_approval":
        return f"{use.describe()} requires approval under: {c.canonical_text}"
    if outcome == "incomparable":
        return f"{use.describe()} cannot be compared with: {c.canonical_text}"
    return f"{use.describe()} violates: {c.canonical_text}"


class ActionTier:
    name: TierId = "T1a"

    def applies(self, ctx: CheckContext) -> bool:
        action = ctx.request.proposed_action
        return action is not None and action.binding is not None

    async def run(self, ctx: CheckContext) -> TierOutcome:
        action = ctx.request.proposed_action
        assert action is not None
        assert action.binding is not None  # guaranteed by applies()
        binding = action.binding
        relevant = [
            c
            for c in ctx.registered
            if (c.action, c.resource) == (binding.action, binding.resource)
        ]
        if not relevant:
            return TierOutcome()

        use = _Use(action, binding)
        if use.problems:
            # Fail-safe (§5): an action we cannot read is not shown to be within bounds.
            return TierOutcome(
                findings=[
                    Finding(
                        Failure(
                            kind="ACTION_VIOLATION",
                            constraint_id=c.constraint_id,
                            detail=f"{action.tool}: " + "; ".join(use.problems),
                        )
                    )
                    for c in relevant
                ],
                terminal=True,
            )

        (eff,) = compose(relevant).effective
        combined = use.evaluate(eff, ctx.now)
        if combined == "allowed":
            return TierOutcome()

        findings = []
        for c in relevant:
            (single,) = compose([c]).effective
            outcome = use.evaluate(single, ctx.now)
            if outcome != "allowed":
                findings.append(
                    Finding(
                        Failure(
                            kind="ACTION_VIOLATION",
                            constraint_id=c.constraint_id,
                            detail=f"{action.tool}: {_reason(outcome, use, c)}",
                        ),
                        requires_approval=outcome == "needs_approval",
                    )
                )
        if not findings:
            # Only the combination fails (e.g. units mixed across constraints).
            findings.append(
                Finding(
                    Failure(
                        kind="ACTION_VIOLATION",
                        constraint_id=relevant[0].constraint_id,
                        detail=f"{action.tool}: {use.describe()} fails the combined constraints",
                    ),
                    requires_approval=combined == "needs_approval",
                )
            )
        return TierOutcome(findings=findings, terminal=True)
