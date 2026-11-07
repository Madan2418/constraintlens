from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from constraintlens.domain.canonical import (
    canonical_datetime,
    canonical_decimal,
    canonical_json,
    compute_content_hash,
    compute_envelope_digest,
    issue,
    render_canonical_text,
    verify_content_hash,
)
from constraintlens.domain.models import ConstraintSpec, Limit
from constraintlens.domain.subject import SubjectScope
from tests.factories import T0, make_constraint

CID = UUID("7d2e1b4a-90c3-4f5e-8a21-6b3c9d0e1f21")


def spec(**kw: object) -> ConstraintSpec:
    fields: dict[str, object] = {
        "owner": "user:alice",
        "action": "refund",
        "resource": "order",
        "subject": "order:4471",
        "limit": Limit(max_value=Decimal(100), unit="USD"),
        "authority": "requires_approval_above_limit",
    }
    fields.update(kw)
    return ConstraintSpec(**fields)  # type: ignore[arg-type]


class TestSubjectScope:
    @pytest.mark.parametrize(
        ("text", "formatted"),
        [
            ("*", "*"),
            ("Order:4471", "order:4471"),
            ("{order:2, order:1}", "{order:1,order:2}"),
            ("{order:1}", "order:1"),
            ("order:*", "order:*"),
        ],
    )
    def test_parse_and_format(self, text: str, formatted: str) -> None:
        assert SubjectScope.parse(text).format() == formatted

    @pytest.mark.parametrize("bad", ["", "{}", "{a,}", "{a,*}", "a,b", "{a{b}"])
    def test_malformed(self, bad: str) -> None:
        with pytest.raises(ValueError, match="malformed"):
            SubjectScope.parse(bad)

    def test_intersection_rules(self) -> None:
        p = SubjectScope.parse
        assert p("*").intersect(p("order:1")) == p("order:1")
        assert p("order:1").intersect(p("*")) == p("order:1")
        assert p("order:*").intersect(p("{order:1,invoice:2}")) == p("order:1")
        assert p("{order:1,invoice:2}").intersect(p("order:*")) == p("order:1")
        assert p("order:1").intersect(p("order:2")).is_empty
        assert p("order:*").intersect(p("order:*")) == p("order:*")

    def test_contains_and_subset(self) -> None:
        p = SubjectScope.parse
        assert p("order:*").contains("ORDER:9")
        assert not p("order:1").contains("order:2")
        assert p("*").contains("anything")
        assert p("order:1").is_subset_of(p("order:*"))
        assert p("order:1").is_subset_of(p("*"))
        assert not p("*").is_subset_of(p("order:1"))
        assert not p("order:*").is_subset_of(p("order:1"))

    def test_constraint_rejects_malformed_subject(self) -> None:
        with pytest.raises(ValidationError, match="malformed subject"):
            make_constraint(subject="{}")


class TestPrimitives:
    @pytest.mark.parametrize(
        ("value", "text"),
        [("100.00", "100"), ("1E+3", "1000"), ("0.50", "0.5"), ("-0", "0"), ("0", "0")],
    )
    def test_canonical_decimal(self, value: str, text: str) -> None:
        assert canonical_decimal(Decimal(value)) == text

    def test_non_finite_decimal_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            canonical_decimal(Decimal("NaN"))

    def test_canonical_datetime_normalises_to_utc(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        assert canonical_datetime(datetime(2026, 11, 7, 14, 30, tzinfo=ist)) == (
            "2026-11-07T09:00:00.000000Z"
        )
        with pytest.raises(ValueError, match="timezone-aware"):
            canonical_datetime(datetime(2026, 11, 7))

    def test_canonical_json_is_order_independent(self) -> None:
        a = canonical_json({"b": [Decimal("1.0"), CID], "a": None, "c": True})
        b = canonical_json({"c": True, "a": None, "b": [Decimal("1"), CID]})
        assert a == b == f'{{"a":null,"b":["1","{CID}"],"c":true}}'


class TestCanonicalText:
    def test_worked_example(self) -> None:
        assert render_canonical_text(spec()) == (
            "refund (order) for order:4471: amount must not exceed 100 USD; "
            "any amount above 100 USD requires approval."
        )

    def test_variants(self) -> None:
        assert render_canonical_text(
            spec(
                subject="*",
                limit=Limit(max_value=Decimal(50), min_value=Decimal(5), unit="USD"),
                authority="requires_approval_always",
                expiry=T0,
            )
        ) == (
            "refund (order): amount must be between 5 USD and 50 USD; approval is required "
            "before execution; valid until 2026-11-07T09:00:00.000000Z."
        )
        assert (
            render_canonical_text(
                spec(
                    limit=Limit(max_value=None, min_value=Decimal(10), unit="USD"), authority="none"
                )
            )
            == "refund (order) for order:4471: amount must be at least 10 USD."
        )
        assert render_canonical_text(spec(limit=None, authority="none")) == (
            "refund (order) for order:4471 only."
        )
        assert render_canonical_text(spec(subject="*", limit=None, authority="none")) == (
            "refund (order)."
        )


class TestIssueAndHash:
    def test_issue_fills_derived_fields_and_verifies(self) -> None:
        c = issue(spec(), constraint_id=CID, issued_at=T0)
        assert c.constraint_id == CID
        assert c.canonical_text.startswith("refund (order)")
        assert verify_content_hash(c)
        assert c.spec() == spec()

    def test_equal_meaning_equal_hash(self) -> None:
        a = issue(
            spec(limit=Limit(max_value=Decimal("100.00"), unit="USD"), subject="ORDER:4471"),
            constraint_id=CID,
            issued_at=T0,
        )
        ist = timezone(timedelta(hours=5, minutes=30))
        b = issue(
            spec(limit=Limit(max_value=Decimal("1E+2"), unit="USD")),
            constraint_id=CID,
            issued_at=T0.astimezone(ist),
        )
        assert a.content_hash == b.content_hash
        assert a.limit is not None
        assert str(a.limit.max_value) == "100"

    @pytest.mark.parametrize(
        "change",
        [
            {"limit": Limit(max_value=Decimal(1000), unit="USD")},
            {"authority": "none"},
            {"subject": "order:9999"},
            {"owner": "user:mallory"},
            {"issued_at": T0 + timedelta(seconds=1)},
            {"canonical_text": "refund anything."},
        ],
    )
    def test_any_tamper_breaks_the_hash(self, change: dict[str, object]) -> None:
        c = issue(spec(), constraint_id=CID, issued_at=T0)
        tampered = c.model_copy(update=change)
        assert not verify_content_hash(tampered)
        assert compute_content_hash(tampered) != c.content_hash

    def test_envelope_digest(self) -> None:
        a = issue(spec(), constraint_id=CID, issued_at=T0)
        b = issue(spec(action="transfer"), constraint_id=UUID(int=1), issued_at=T0)
        assert compute_envelope_digest(CID, [a, b]) == compute_envelope_digest(CID, [b, a])
        assert compute_envelope_digest(CID, [a, b]) != compute_envelope_digest(CID, [a])
        assert compute_envelope_digest(CID, [a]) != compute_envelope_digest(UUID(int=2), [a])

    def test_issued_constraint_is_utc(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        c = issue(
            spec(expiry=T0.astimezone(ist) + timedelta(days=1)),
            constraint_id=CID,
            issued_at=T0.astimezone(ist),
        )
        assert c.issued_at.tzinfo == UTC
        assert c.expiry is not None
        assert c.expiry.tzinfo == UTC
