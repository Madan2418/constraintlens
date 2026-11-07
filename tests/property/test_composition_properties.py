"""Phase 1 exit criterion: composition property tests (§20).

Main invariant: the effective constraint is never looser than any input constraint,
for any amount, unit, subject and time.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from hypothesis import given, settings
from hypothesis import strategies as st

from constraintlens.domain.canonical import issue, verify_content_hash
from constraintlens.domain.composition import OUTCOME_RANK, compose
from constraintlens.domain.models import Constraint, ConstraintSpec, Limit
from constraintlens.domain.subject import SubjectScope
from tests.factories import T0

UNITS = ["USD", "INR"]
ITEMS = ["order:1", "order:2", "invoice:7"]
SUBJECTS = ["*", "order:*", "order:1", "order:2", "{order:1,invoice:7}", "invoice:*"]

amounts = st.integers(min_value=0, max_value=1000).map(Decimal)


@st.composite
def limits(draw: st.DrawFn) -> Limit | None:
    if draw(st.booleans()):
        return None
    lo = draw(st.none() | amounts)
    hi = draw(st.none() | amounts)
    if lo is None and hi is None:
        hi = draw(amounts)
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return Limit(max_value=hi, min_value=lo, unit=draw(st.sampled_from(UNITS)))


@st.composite
def constraints(draw: st.DrawFn) -> Constraint:
    limit = draw(limits())
    authorities = ["none", "requires_approval_always"]
    if limit is not None and limit.max_value is not None:
        authorities.append("requires_approval_above_limit")
    spec = ConstraintSpec(
        scope=draw(st.sampled_from(["user", "org", "system"])),
        owner="p",
        subject=draw(st.sampled_from(SUBJECTS)),
        action="refund",
        resource="order",
        limit=limit,
        authority=draw(st.sampled_from(authorities)),
        expiry=draw(st.none() | st.integers(1, 48).map(lambda h: T0 + timedelta(hours=h))),
    )
    return issue(spec, constraint_id=uuid4(), issued_at=T0)


uses = st.fixed_dictionaries(
    {
        "amount": st.none() | amounts,
        "unit": st.sampled_from(UNITS),
        "subject_item": st.none() | st.sampled_from(ITEMS),
        "at": st.integers(0, 60).map(lambda h: T0 + timedelta(hours=h)),
    }
)


def _evaluate(cs: list[Constraint], use: dict[str, object]) -> str:
    (eff,) = compose(cs).effective
    return eff.evaluate(**use)  # type: ignore[arg-type]


@settings(max_examples=300)
@given(cs=st.lists(constraints(), min_size=1, max_size=5), use=uses)
def test_effective_never_looser_than_any_input(
    cs: list[Constraint], use: dict[str, object]
) -> None:
    combined = OUTCOME_RANK[_evaluate(cs, use)]
    for c in cs:
        assert combined >= OUTCOME_RANK[_evaluate([c], use)], (c.canonical_text, use)


@given(cs=st.lists(constraints(), min_size=1, max_size=5), seed=st.randoms())
def test_composition_is_order_independent(cs: list[Constraint], seed: object) -> None:
    shuffled = list(cs)
    seed.shuffle(shuffled)  # type: ignore[attr-defined]
    a, b = compose(cs).effective[0], compose(shuffled).effective[0]
    assert a.model_dump(exclude={"constraint_ids"}) == b.model_dump(exclude={"constraint_ids"})
    assert sorted(a.constraint_ids) == sorted(b.constraint_ids)


@given(cs=st.lists(constraints(), min_size=1, max_size=4))
def test_composition_is_idempotent(cs: list[Constraint]) -> None:
    once = compose(cs).effective[0]
    twice = compose(cs + cs).effective[0]
    assert once.model_dump(exclude={"constraint_ids"}) == twice.model_dump(
        exclude={"constraint_ids"}
    )


@given(cs=st.lists(constraints(), min_size=1, max_size=5))
def test_adding_a_constraint_never_loosens(cs: list[Constraint]) -> None:
    use = {"amount": Decimal(500), "unit": "USD", "subject_item": "order:1", "at": T0}
    before = OUTCOME_RANK[_evaluate(cs[:-1], use)] if len(cs) > 1 else 0
    assert OUTCOME_RANK[_evaluate(cs, use)] >= before


@given(cs=st.lists(constraints(), min_size=1, max_size=5))
def test_unsatisfiable_groups_admit_nothing(cs: list[Constraint]) -> None:
    comp = compose(cs)
    if not comp.unsatisfiable:
        return
    (eff,) = comp.effective
    for item in ITEMS:
        for amt in (Decimal(0), Decimal(500), Decimal(1000)):
            for unit in UNITS:
                outcome = eff.evaluate(at=T0, amount=amt, unit=unit, subject_item=item)
                assert outcome in {"forbidden", "incomparable"}


@given(c=constraints())
def test_issued_constraints_hash_and_round_trip(c: Constraint) -> None:
    assert verify_content_hash(c)
    again = Constraint.model_validate_json(c.model_dump_json())
    assert again == c
    assert verify_content_hash(again)


@given(a=st.sampled_from(SUBJECTS), b=st.sampled_from(SUBJECTS), item=st.sampled_from(ITEMS))
def test_subject_intersection_is_exact(a: str, b: str, item: str) -> None:
    sa, sb = SubjectScope.parse(a), SubjectScope.parse(b)
    both = sa.intersect(sb)
    assert both.contains(item) == (sa.contains(item) and sb.contains(item))
    assert both == sb.intersect(sa)
