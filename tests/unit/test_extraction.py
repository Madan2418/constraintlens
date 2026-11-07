from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from constraintlens.adapters.judge.scripted import ScriptedJudge
from constraintlens.domain.errors import DependencyUnavailable
from constraintlens.domain.extraction import (
    ExtractionContext,
    extract,
    extract_rules,
    parse_llm_output,
    reconcile,
)
from constraintlens.domain.models import ConstraintSpec, Limit

ISSUED = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
CTX = ExtractionContext(owner="user:alice", issued_at=ISSUED, max_age_seconds=3600)


def rules(text: str) -> list[ConstraintSpec]:
    return extract_rules(text, CTX)[0]


def llm_json(*items: dict[str, object]) -> str:
    return json.dumps({"constraints": list(items)})


REFUND_100 = {
    "action": "refund",
    "resource": "order",
    "subject": "*",
    "limit": {"max_value": "100", "min_value": None, "unit": "USD"},
    "authority": "requires_approval_above_limit",
    "expiry": None,
}


class TestRules:
    def test_worked_example_and_context_fields(self) -> None:
        (s,) = rules("Refund this customer, up to $100. Anything above needs manager approval.")
        assert (s.action, s.resource, s.subject) == ("refund", "order", "*")
        assert s.limit == Limit(max_value=Decimal(100), unit="USD")
        assert s.authority == "requires_approval_above_limit"
        assert (s.owner, s.scope, s.origin, s.max_age_seconds) == (
            "user:alice",
            "user",
            "user",
            3600,
        )

    def test_no_restriction_no_constraint(self) -> None:
        assert rules("Refund order #4471 in full.") == []
        assert rules("Translate the guide.") == []
        assert rules("Refund order #888 for $40.") == []  # a bare amount is not a limit

    def test_cap_and_threshold_become_two_constraints(self) -> None:
        caps = rules("Transfer up to $5,000, but transfers above $1,000 need approval.")
        assert sorted((s.authority, s.limit.max_value) for s in caps if s.limit) == [
            ("none", Decimal(5000)),
            ("requires_approval_above_limit", Decimal(1000)),
        ]

    def test_amount_forms(self) -> None:
        cases = {
            "Wire up to 7.5k EUR to the supplier.": (Decimal(7500), "EUR"),
            "Transfer Rs 20000 at most.": (Decimal(20000), "INR"),
            "Transfer up to 2 lakh rupees.": (Decimal(200000), "INR"),
            "Refund no more than 75 dollars.": (Decimal(75), "USD"),
            "Buy chairs for at most £1,200.": (Decimal(1200), "GBP"),
            "Export no more than 1k rows.": (Decimal(1000), "records"),
        }
        for text, (value, unit) in cases.items():
            (s,) = rules(text)
            assert s.limit is not None, text
            assert (s.limit.max_value, s.limit.unit) == (value, unit), text

    def test_negated_over_is_a_max_and_bare_over_is_ignored(self) -> None:
        (s,) = rules("Please refund order 5520 but don't go over $250.")
        assert s.limit is not None
        assert s.limit.max_value == Decimal(250)
        assert rules("Refund amounts over $10 to loyal customers.") == []

    def test_explicit_no_approval(self) -> None:
        (s,) = rules("Refund order #77 with no approval needed, capped at $20.")
        assert s.authority == "none"

    def test_approval_always_scope_and_expiry(self) -> None:
        (s,) = rules("Retrieve the files for project Orion, but only with my approval.")
        assert (s.action, s.subject, s.authority) == (
            "access",
            "project:orion",
            "requires_approval_always",
        )
        (e,) = rules("Erase customer C-777's data within 30 days as requested.")
        assert e.expiry == datetime(2026, 12, 7, 9, 0, tzinfo=UTC)
        (d,) = rules("Remit ₹50,000 maximum to the landlord by 2026-11-10.")
        assert d.expiry == datetime(2026, 11, 10, 23, 59, 59, tzinfo=UTC)
        (t,) = rules("Refund order #6060, maximum 1.5k INR, by 2026-11-08T17:00.")
        assert t.expiry == datetime(2026, 11, 8, 17, 0, tzinfo=UTC)
        (w,) = rules("Delete at most 5 files within 2 weeks.")
        assert w.expiry == datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
        (m,) = rules("Delete at most 5 files within 30 minutes.")
        assert m.expiry == datetime(2026, 11, 7, 9, 30, tzinfo=UTC)

    def test_scope_only_constraint(self) -> None:
        (s,) = rules("Delete records for order #5150 only.")
        assert (s.subject, s.limit, s.authority) == ("order:5150", None, "none")

    def test_multiple_actions_and_pre_action_subject(self) -> None:
        specs = rules(
            "Refund order #1212 up to $30, and transfer no more than $500 to account 9090."
        )
        assert [(s.action, s.subject) for s in specs] == [
            ("refund", "order:1212"),
            ("transfer", "account:9090"),
        ]
        (s,) = rules("Customer on order #1188 wants their money back — cap the refund at $120.")
        assert s.subject == "order:1188"

    def test_order_as_verb(self) -> None:
        (s,) = rules("Order 20 laptops, budget of $15,000.")
        assert (s.action, s.subject) == ("purchase", "*")

    def test_duplicate_limits_keep_strictest_and_flag(self) -> None:
        specs, flags = extract_rules("Refund at most $50. Refunds must not exceed $40.", CTX)
        (s,) = specs
        assert s.limit is not None
        assert s.limit.max_value == Decimal(40)
        assert [f.kind for f in flags] == ["DUPLICATE_LIMIT"]

    def test_approval_reference_without_amount(self) -> None:
        (s,) = rules("Purchase chairs; anything above needs approval.")
        assert s.authority == "requires_approval_always"


class TestLLMParsing:
    def test_valid_output(self) -> None:
        (s,) = parse_llm_output(llm_json(REFUND_100), CTX)
        assert s.limit is not None
        assert s.limit.max_value == Decimal(100)
        assert s.owner == "user:alice"

    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            ("not json", "not valid JSON"),
            (json.dumps({"constraints": [{**REFUND_100, "bogus": 1}]}), "schema validation"),
            (json.dumps({"items": []}), "schema validation"),
            (llm_json({**REFUND_100, "expiry": "2020-01-01T00:00:00Z"}), "not after the issue"),
            (llm_json({**REFUND_100, "subject": "{}"}), "malformed subject"),
        ],
    )
    def test_invalid_output(self, raw: str, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            parse_llm_output(raw, CTX)


class TestPipeline:
    async def test_rules_only_never_calls_judge(self) -> None:
        judge = ScriptedJudge()
        res = await extract("Refund up to $100.", CTX, judge=judge, llm_mode="off")
        assert res.sources == ["rule"]
        assert judge.extraction_calls == []

    async def test_fallback_only_when_rules_find_nothing(self) -> None:
        text = "Refund the client, keeping it under a hundred dollars."
        judge = ScriptedJudge(extractions={text: llm_json(REFUND_100)})
        res = await extract(text, CTX, judge=judge, llm_mode="fallback")
        assert res.llm_called
        assert res.sources == ["llm"]
        assert len(judge.extraction_calls) == 1
        judge2 = ScriptedJudge()
        await extract("Refund up to $100.", CTX, judge=judge2, llm_mode="fallback")
        assert judge2.extraction_calls == []

    async def test_repair_retry_then_success(self) -> None:
        text = "Refund it, a hundred bucks tops."
        judge = ScriptedJudge(extractions={text: ["oops", llm_json(REFUND_100)]})
        res = await extract(text, CTX, judge=judge, llm_mode="always")
        assert not res.degraded
        assert len(res.specs) == 1
        assert judge.extraction_calls[0][1] is None
        assert "not valid JSON" in (judge.extraction_calls[1][1] or "")

    async def test_rejected_after_one_repair(self) -> None:
        text = "Refund it."
        judge = ScriptedJudge(extractions={text: ["bad", "still bad"]})
        res = await extract(text, CTX, judge=judge, llm_mode="always")
        assert res.degraded
        assert [f.kind for f in res.flags] == ["LLM_REJECTED"]
        assert len(judge.extraction_calls) == 2

    async def test_judge_unavailable_degrades_to_rules(self) -> None:
        judge = ScriptedJudge(fail_with=DependencyUnavailable("judge", "timeout"))
        res = await extract("Refund up to $100.", CTX, judge=judge, llm_mode="always")
        assert res.degraded
        assert res.sources == ["rule"]
        assert [f.kind for f in res.flags] == ["LLM_UNAVAILABLE"]

    async def test_enrichment_reconciles_to_more_restrictive(self) -> None:
        text = "Refund order #12 up to $100."
        llm = {
            **REFUND_100,
            "subject": "order:12",
            "limit": {"max_value": "80", "min_value": None, "unit": "USD"},
            "expiry": "2026-11-08T09:00:00Z",
        }
        judge = ScriptedJudge(extractions={text: llm_json(llm)})
        res = await extract(text, CTX, judge=judge, llm_mode="always")
        (s,) = res.specs
        assert res.sources == ["both"]
        assert s.limit is not None
        assert s.limit.max_value == Decimal(80)
        # rule said none (hard cap); LLM said approval above: hard cap is stricter
        assert s.authority == "none"
        assert s.expiry == datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
        kinds = sorted(f.kind for f in res.flags)
        assert kinds == ["FIELD_DISAGREEMENT", "LIMIT_DISAGREEMENT"]


class TestReconcile:
    def _spec(self, **kw: object) -> ConstraintSpec:
        fields: dict[str, object] = {"owner": "u", "action": "refund", "resource": "order"}
        fields.update(kw)
        return ConstraintSpec(**fields)  # type: ignore[arg-type]

    def test_unit_disagreement_keeps_rule(self) -> None:
        r = self._spec(limit=Limit(max_value=Decimal(1), unit="USD"))
        m = self._spec(limit=Limit(max_value=Decimal(1), unit="EUR"))
        specs, sources, flags = reconcile([r], [m])
        assert specs == [r]
        assert sources == ["both"]
        assert flags[0].kind == "UNIT_DISAGREEMENT"

    def test_only_one_side_has_limit(self) -> None:
        r = self._spec(authority="requires_approval_always")
        m = self._spec(limit=Limit(max_value=Decimal(5), unit="USD"))
        (s,), _, flags = reconcile([r], [m])
        assert s.limit == m.limit
        assert s.authority == "requires_approval_always"
        assert "LIMIT_DISAGREEMENT" in [f.kind for f in flags]

    def test_strictest_authority_and_always_fallback(self) -> None:
        r = self._spec(limit=Limit(max_value=None, min_value=Decimal(5), unit="USD"))
        m = self._spec(
            limit=Limit(max_value=Decimal(50), unit="USD"),
            authority="requires_approval_above_limit",
        )
        (s,), _, _ = reconcile([r], [m])
        assert s.limit is not None
        assert (s.limit.min_value, s.limit.max_value) == (Decimal(5), Decimal(50))
        # "none" (hard cap) is stricter than "approval above the cap"
        assert s.authority == "none"
        r2 = self._spec(limit=None)
        m2 = self._spec(limit=None, authority="requires_approval_always")
        (s2,), _, _ = reconcile([r2], [m2])
        assert s2.authority == "requires_approval_always"

    def test_empty_bounds_and_disjoint_subjects(self) -> None:
        r = self._spec(subject="order:1", limit=Limit(max_value=Decimal(10), unit="USD"))
        m = self._spec(
            subject="order:2", limit=Limit(max_value=Decimal(50), min_value=Decimal(20), unit="USD")
        )
        (s,), _, flags = reconcile([r], [m])
        assert s.limit is not None
        assert s.limit.min_value is None
        assert s.subject == "order:1"
        assert any("do not overlap" in f.detail for f in flags)

    def test_many_to_many_keeps_all(self) -> None:
        a = self._spec(limit=Limit(max_value=Decimal(1), unit="USD"))
        b = self._spec(limit=Limit(max_value=Decimal(2), unit="USD"))
        c = self._spec(action="delete", resource="records")
        specs, sources, flags = reconcile([a, b], [a, c])
        assert len(specs) == 4
        assert sorted(sources) == ["llm", "llm", "rule", "rule"]
        assert [f.kind for f in flags] == ["AMBIGUOUS_MATCH"]
