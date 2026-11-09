"""§20 "Performance": fail CI on a latency-budget regression for Tiers 0 to 1b (§10.3)."""

from __future__ import annotations

from eval.latency_budget import measure, render


async def test_tiers_0_to_1b_and_end_to_end_within_budget() -> None:
    report = await measure(iterations=5)
    for tier in ("T0", "T1a", "T1b"):
        stats = report["tiers"][tier]
        assert stats["within"], f"{tier} p95 {stats['p95']} ms over {stats['budget_p95']} ms"
    assert report["end_to_end_no_escalation"]["within"]
    assert {"T1c", "T2"} <= set(report["tiers"])  # routing to the semantic tiers is exercised
    assert "| T1b |" in render(report)
