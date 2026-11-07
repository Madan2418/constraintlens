"""Constraint composition (ARCHITECTURE.md §7.6, ADR 0003).

* **Tracking.** Constraints are kept individually; ``find_dropped`` names each one
  missing from an envelope.
* **Resolution.** ``compose`` groups constraints by ``(action, resource)`` and
  derives one ``EffectiveConstraint`` per group, most-restrictive-wins per dimension:

  ===================  ===============================================
  numeric limit        hard cap = min, hard floor = max (per unit)
  approval             threshold = min; "always" if any requires it
  resource scope       subject intersection
  expiry               earliest
  ===================  ===============================================

  Hard caps (authority ``none`` / ``requires_approval_always``) and approval
  thresholds (``requires_approval_above_limit``) are kept apart. Merging them would
  let an org hard cap of 50 become "approval above 50", which is looser than the org
  rule. Invariant (property-tested): the effective constraint is never looser than any
  input, for every amount, subject and time.
* **Conflicts.** ``SUPERSEDED`` when a lower-precedence constraint (user < org < system)
  is looser than a higher one (reporting only; enforcement is unaffected).
  ``UNSATISFIABLE`` when a group admits no action at all. ``INCOMPARABLE_UNITS``
  when one group bounds amounts in different units.

Pure module.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from constraintlens.domain.canonical import canonical_decimal
from constraintlens.domain.models import Constraint, Failure, _Frozen
from constraintlens.domain.subject import SubjectScope

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

Outcome = Literal["allowed", "needs_approval", "forbidden", "incomparable"]
ConflictKind = Literal["SUPERSEDED", "UNSATISFIABLE", "INCOMPARABLE_UNITS"]

#: Strictness order. "incomparable" (amount in a unit the constraint cannot compare)
#: ranks with "forbidden": fail-safe default (§5).
OUTCOME_RANK: dict[str, int] = {
    "allowed": 0,
    "needs_approval": 1,
    "forbidden": 2,
    "incomparable": 2,
}
SCOPE_PRECEDENCE: dict[str, int] = {"user": 0, "org": 1, "system": 2}
EMPTY_SUBJECT = "{}"


class EffectiveConstraint(_Frozen):
    """The resolved restriction for one ``(action, resource)`` group."""

    action: str
    resource: str
    subject: str  # canonical SubjectScope text
    hard_max: dict[str, Decimal] = Field(default_factory=dict)  # per unit
    hard_min: dict[str, Decimal] = Field(default_factory=dict)
    approval_above: dict[str, Decimal] = Field(default_factory=dict)
    approval_always: bool = False
    expiry: AwareDatetime | None = None
    constraint_ids: list[UUID]

    @property
    def scope(self) -> SubjectScope:
        # "{}" marks an empty intersection (an UNSATISFIABLE group): it contains nothing.
        if self.subject == EMPTY_SUBJECT:
            return SubjectScope(frozenset())
        return SubjectScope.parse(self.subject)

    @property
    def units(self) -> set[str]:
        return set(self.hard_max) | set(self.hard_min) | set(self.approval_above)

    def evaluate(
        self,
        *,
        at: datetime,
        amount: Decimal | None = None,
        unit: str | None = None,
        subject_item: str | None = None,
    ) -> Outcome:
        """Strictest outcome for a proposed use. Basis for the Tier 1a check (Phase 3)."""
        if self.expiry is not None and at > self.expiry:
            return "forbidden"
        if subject_item is not None and not self.scope.contains(subject_item):
            return "forbidden"
        outcome: Outcome = "needs_approval" if self.approval_always else "allowed"
        if amount is None:
            return outcome
        if unit is None:
            raise ValueError("an amount needs a unit")
        if unit in self.hard_max and amount > self.hard_max[unit]:
            return "forbidden"
        if unit in self.hard_min and amount < self.hard_min[unit]:
            return "forbidden"
        # Any bound in another unit cannot be checked without conversion, so the amount
        # cannot be shown to satisfy it (fail-safe, §5). Found by the never-looser property.
        if self.units - {unit}:
            return "incomparable"
        if unit in self.approval_above and amount > self.approval_above[unit]:
            return "needs_approval"
        return outcome


class Conflict(_Frozen):
    kind: ConflictKind
    constraint_id: UUID  # the constraint the conflict is reported on
    other_id: UUID | None = None  # the stricter constraint, for SUPERSEDED
    dimension: str
    detail: str


class Composition(_Frozen):
    effective: list[EffectiveConstraint]
    conflicts: list[Conflict]

    @property
    def unsatisfiable(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.kind == "UNSATISFIABLE"]

    def for_action(self, action: str, resource: str) -> EffectiveConstraint | None:
        for e in self.effective:
            if (e.action, e.resource) == (action, resource):
                return e
        return None


# --------------------------------------------------------------------------- profile


class _Profile:
    """One constraint's restrictions, split into the composable dimensions."""

    def __init__(self, c: Constraint) -> None:
        self.c = c
        self.subject = SubjectScope.parse(c.subject)
        self.hard_max: dict[str, Decimal] = {}
        self.hard_min: dict[str, Decimal] = {}
        self.approval_above: dict[str, Decimal] = {}
        self.approval_always = c.authority == "requires_approval_always"
        lim = c.limit
        if lim is not None:
            if lim.min_value is not None:
                self.hard_min[lim.unit] = lim.min_value
            if lim.max_value is not None:
                if c.authority == "requires_approval_above_limit":
                    self.approval_above[lim.unit] = lim.max_value
                else:
                    self.hard_max[lim.unit] = lim.max_value


def _merge_min(into: dict[str, Decimal], new: dict[str, Decimal]) -> None:
    for unit, v in new.items():
        into[unit] = min(into.get(unit, v), v)


def _merge_max(into: dict[str, Decimal], new: dict[str, Decimal]) -> None:
    for unit, v in new.items():
        into[unit] = max(into.get(unit, v), v)


# --------------------------------------------------------------------------- compose


def compose(constraints: Iterable[Constraint]) -> Composition:
    groups: dict[tuple[str, str], list[_Profile]] = defaultdict(list)
    for c in constraints:
        groups[(c.action, c.resource)].append(_Profile(c))

    effective: list[EffectiveConstraint] = []
    conflicts: list[Conflict] = []
    for (action, resource), profiles in sorted(groups.items()):
        eff, group_conflicts = _resolve(action, resource, profiles)
        effective.append(eff)
        conflicts.extend(group_conflicts)
        conflicts.extend(_superseded(profiles))
    return Composition(effective=effective, conflicts=conflicts)


def _resolve(
    action: str, resource: str, profiles: list[_Profile]
) -> tuple[EffectiveConstraint, list[Conflict]]:
    subject = SubjectScope.everything()
    hard_max: dict[str, Decimal] = {}
    hard_min: dict[str, Decimal] = {}
    approval_above: dict[str, Decimal] = {}
    expiry: datetime | None = None
    for p in profiles:
        subject = subject.intersect(p.subject)
        _merge_min(hard_max, p.hard_max)
        _merge_max(hard_min, p.hard_min)
        _merge_min(approval_above, p.approval_above)
        if p.c.expiry is not None:
            expiry = p.c.expiry if expiry is None else min(expiry, p.c.expiry)

    ids = [p.c.constraint_id for p in profiles]
    eff = EffectiveConstraint(
        action=action,
        resource=resource,
        subject=EMPTY_SUBJECT if subject.is_empty else subject.format(),
        hard_max=hard_max,
        hard_min=hard_min,
        approval_above=approval_above,
        approval_always=any(p.approval_always for p in profiles),
        expiry=expiry,
        constraint_ids=ids,
    )

    conflicts: list[Conflict] = []
    first = profiles[0].c.constraint_id
    label = f"{action} ({resource})"
    if subject.is_empty:
        conflicts.append(
            Conflict(
                kind="UNSATISFIABLE",
                constraint_id=first,
                dimension="subject",
                detail=f"{label}: subject scopes "
                + ", ".join(sorted(p.c.subject for p in profiles))
                + " have no common item",
            )
        )
    for unit in sorted(set(hard_max) & set(hard_min)):
        if hard_min[unit] > hard_max[unit]:
            conflicts.append(
                Conflict(
                    kind="UNSATISFIABLE",
                    constraint_id=first,
                    dimension="limit",
                    detail=f"{label}: minimum {canonical_decimal(hard_min[unit])} {unit} "
                    f"exceeds maximum {canonical_decimal(hard_max[unit])} {unit}",
                )
            )
    if len(eff.units) > 1:
        conflicts.append(
            Conflict(
                kind="INCOMPARABLE_UNITS",
                constraint_id=first,
                dimension="limit",
                detail=f"{label}: limits in {', '.join(sorted(eff.units))} cannot be compared; "
                "each is enforced separately and amounts in other units are refused",
            )
        )
    return eff, conflicts


def _looser_dimensions(lo: _Profile, hi: _Profile) -> list[str]:
    """Dimensions on which ``lo`` admits something ``hi`` does not."""
    out: list[str] = []
    looser_max = any(u not in lo.hard_max or lo.hard_max[u] > v for u, v in hi.hard_max.items())
    looser_min = any(u not in lo.hard_min or lo.hard_min[u] < v for u, v in hi.hard_min.items())
    if looser_max or looser_min:
        out.append("limit")
    if (hi.approval_always and not lo.approval_always) or any(
        not lo.approval_always
        and (u not in lo.approval_above or lo.approval_above[u] > v)
        and not (u in lo.hard_max and lo.hard_max[u] <= v)
        for u, v in hi.approval_above.items()
    ):
        out.append("authority")
    if not lo.subject.is_subset_of(hi.subject):
        out.append("subject")
    if hi.c.expiry is not None and (lo.c.expiry is None or lo.c.expiry > hi.c.expiry):
        out.append("expiry")
    return out


def _superseded(profiles: list[_Profile]) -> list[Conflict]:
    out: list[Conflict] = []
    for lo in profiles:
        for hi in profiles:
            if SCOPE_PRECEDENCE[lo.c.scope] >= SCOPE_PRECEDENCE[hi.c.scope]:
                continue
            for dim in _looser_dimensions(lo, hi):
                out.append(
                    Conflict(
                        kind="SUPERSEDED",
                        constraint_id=lo.c.constraint_id,
                        other_id=hi.c.constraint_id,
                        dimension=dim,
                        detail=f"{lo.c.scope} constraint is looser than {hi.c.scope} "
                        f"constraint on {dim}; the stricter value applies",
                    )
                )
    return out


# --------------------------------------------------------------------------- dropped


def find_dropped(registered: Sequence[Constraint], present: Sequence[Constraint]) -> list[Failure]:
    """Compare an envelope's constraints with the registered set (§7.6, §9.5).

    An envelope with no constraints at all is ``CONSTRAINT_LOST``; otherwise each
    missing registered id is a ``CONSTRAINT_DROPPED`` naming that constraint.
    Unregistered extras are ignored: the service's registered set is authoritative.
    Whether the *text* still entails each constraint is the checker's job (§7.4).
    """
    if registered and not present:
        return [
            Failure(
                kind="CONSTRAINT_LOST",
                constraint_id=None,
                detail=f"envelope carries none of the {len(registered)} registered constraints",
            )
        ]
    present_ids = {c.constraint_id for c in present}
    return [
        Failure(
            kind="CONSTRAINT_DROPPED",
            constraint_id=c.constraint_id,
            detail=f"{c.scope} constraint missing from envelope: {c.canonical_text}",
        )
        for c in registered
        if c.constraint_id not in present_ids
    ]
