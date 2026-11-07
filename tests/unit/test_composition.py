from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from constraintlens.domain.composition import compose, find_dropped
from constraintlens.domain.models import Limit
from tests.factories import T0, make_constraint


def usd(mx: int | None = None, mn: int | None = None, unit: str = "USD") -> Limit:
    return Limit(
        max_value=None if mx is None else Decimal(mx),
        min_value=None if mn is None else Decimal(mn),
        unit=unit,
    )


def user_100_approval() -> object:
    return make_constraint(limit=usd(100), authority="requires_approval_above_limit")


class TestResolution:
    def test_worked_example_user_and_org(self) -> None:
        user = make_constraint(limit=usd(100), authority="requires_approval_above_limit")
        org = make_constraint(scope="org", subject="*", limit=usd(50), authority="none")
        comp = compose([user, org])
        (eff,) = comp.effective
        assert eff.hard_max == {"USD": Decimal(50)}
        assert eff.approval_above == {"USD": Decimal(100)}
        assert eff.subject == "order:4471"
        # Org hard cap stays hard: 60 USD is forbidden, not "approval required".
        assert eff.evaluate(at=T0, amount=Decimal(60), unit="USD") == "forbidden"
        assert eff.evaluate(at=T0, amount=Decimal(50), unit="USD") == "allowed"
        assert eff.evaluate(at=T0, amount=Decimal(1), unit="USD", subject_item="order:9") == (
            "forbidden"
        )
        assert [(c.kind, c.dimension) for c in comp.conflicts] == [("SUPERSEDED", "limit")]
        assert comp.conflicts[0].constraint_id == user.constraint_id
        assert comp.conflicts[0].other_id == org.constraint_id

    def test_approval_threshold_and_always(self) -> None:
        a = make_constraint(subject="*", limit=usd(500), authority="requires_approval_above_limit")
        b = make_constraint(subject="*", limit=usd(200), authority="requires_approval_above_limit")
        (eff,) = compose([a, b]).effective
        assert eff.approval_above == {"USD": Decimal(200)}
        assert eff.evaluate(at=T0, amount=Decimal(300), unit="USD") == "needs_approval"
        c = make_constraint(subject="*", limit=None, authority="requires_approval_always")
        (eff2,) = compose([a, c]).effective
        assert eff2.approval_always
        assert eff2.evaluate(at=T0) == "needs_approval"
        assert eff2.evaluate(at=T0, amount=Decimal(1), unit="USD") == "needs_approval"

    def test_min_max_expiry_and_groups(self) -> None:
        a = make_constraint(
            subject="*", limit=usd(400, 100), authority="none", expiry=T0 + timedelta(hours=2)
        )
        b = make_constraint(
            subject="*", limit=usd(None, 150), authority="none", expiry=T0 + timedelta(hours=1)
        )
        t = make_constraint(
            subject="*", action="transfer", resource="funds", limit=usd(10), authority="none"
        )
        comp = compose([a, b, t])
        eff = comp.for_action("refund", "order")
        assert eff is not None
        assert eff.hard_min == {"USD": Decimal(150)}
        assert eff.hard_max == {"USD": Decimal(400)}
        assert eff.expiry == T0 + timedelta(hours=1)
        assert eff.evaluate(at=T0, amount=Decimal(120), unit="USD") == "forbidden"
        assert eff.evaluate(at=T0 + timedelta(hours=1, seconds=1)) == "forbidden"
        assert comp.for_action("transfer", "funds") is not None
        assert comp.for_action("delete", "records") is None
        assert comp.conflicts == []

    def test_units_and_missing_unit(self) -> None:
        a = make_constraint(subject="*", limit=usd(100), authority="none")
        b = make_constraint(subject="*", limit=usd(8000, unit="INR"), authority="none")
        comp = compose([a, b])
        (eff,) = comp.effective
        assert eff.evaluate(at=T0, amount=Decimal(5), unit="EUR") == "incomparable"
        assert eff.evaluate(at=T0, amount=Decimal(9000), unit="INR") == "forbidden"
        # Within the INR cap, but the USD cap cannot be checked: still not allowed.
        assert eff.evaluate(at=T0, amount=Decimal(10), unit="INR") == "incomparable"
        assert [c.kind for c in comp.conflicts] == ["INCOMPARABLE_UNITS"]
        import pytest

        with pytest.raises(ValueError, match="needs a unit"):
            eff.evaluate(at=T0, amount=Decimal(5))

    def test_unconstrained_group_allows(self) -> None:
        (eff,) = compose([make_constraint(subject="*", limit=None, authority="none")]).effective
        assert eff.evaluate(at=T0, amount=Decimal(10**9), unit="USD") == "allowed"


class TestConflicts:
    def test_unsatisfiable_subject(self) -> None:
        a = make_constraint(subject="order:1", limit=None, authority="none")
        b = make_constraint(subject="order:2", limit=None, authority="none", scope="org")
        comp = compose([a, b])
        assert [c.dimension for c in comp.unsatisfiable] == ["subject"]
        (eff,) = comp.effective
        assert eff.subject == "{}"
        assert eff.evaluate(at=T0, subject_item="order:1") == "forbidden"

    def test_unsatisfiable_range(self) -> None:
        a = make_constraint(subject="*", limit=usd(50), authority="none")
        b = make_constraint(subject="*", limit=usd(None, 80), authority="none")
        assert "exceeds maximum" in compose([a, b]).unsatisfiable[0].detail

    def test_superseded_dimensions(self) -> None:
        user = make_constraint(subject="*", limit=None, authority="none")
        system = make_constraint(
            scope="system",
            subject="order:*",
            limit=usd(None, 1),
            authority="requires_approval_always",
            expiry=T0 + timedelta(days=1),
        )
        dims = sorted(c.dimension for c in compose([user, system]).conflicts)
        assert dims == ["authority", "expiry", "limit", "subject"]

    def test_stricter_user_is_not_superseded(self) -> None:
        user = make_constraint(subject="order:1", limit=usd(10), authority="none")
        org = make_constraint(
            scope="org", subject="*", limit=usd(50), authority="requires_approval_above_limit"
        )
        assert compose([user, org]).conflicts == []

    def test_user_threshold_vs_org_threshold(self) -> None:
        user = make_constraint(
            subject="*", limit=usd(500), authority="requires_approval_above_limit"
        )
        org = make_constraint(
            scope="org", subject="*", limit=usd(100), authority="requires_approval_above_limit"
        )
        assert [(c.kind, c.dimension) for c in compose([user, org]).conflicts] == [
            ("SUPERSEDED", "authority")
        ]


class TestDropped:
    def test_lost_dropped_and_intact(self) -> None:
        a, b = make_constraint(), make_constraint(scope="org", subject="*")
        assert find_dropped([a, b], [a, b]) == []
        (lost,) = find_dropped([a, b], [])
        assert lost.kind == "CONSTRAINT_LOST"
        assert lost.constraint_id is None
        (dropped,) = find_dropped([a, b], [a])
        assert dropped.kind == "CONSTRAINT_DROPPED"
        assert dropped.constraint_id == b.constraint_id
        assert find_dropped([a], [a, make_constraint(constraint_id=uuid4())]) == []
        assert find_dropped([], []) == []
