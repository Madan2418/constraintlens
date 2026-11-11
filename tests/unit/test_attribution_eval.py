"""The Phase 7 attribution harness (``eval/attribution_accuracy.py``) on generated chains.

The release JSONL is gitignored, so these tests generate a small benchmark in-process.
With the oracle text detector and the age-only staleness slice, attribution must name
the labelled hop of every drifted chain and leave every control alone: this pins the
attribution algorithm independently of how well the rules tiers detect drift.
"""

from __future__ import annotations

import pytest

from driftchain_bench.generator import MAX_AGE_S, generate
from eval.attribution_accuracy import (
    SliceReport,
    render_markdown,
    run_chain,
)

CHAINS = generate(seed=11, size=80)


@pytest.mark.parametrize("chain", CHAINS, ids=lambda c: f"{c.chain_id}-{c.label.mutation_op}")
async def test_oracle_attribution_is_exact(chain: object) -> None:
    from driftchain_bench.record import ChainRec

    assert isinstance(chain, ChainRec)
    result = await run_chain(chain, detector="oracle-text", dwell_s=float(MAX_AGE_S))
    if not chain.label.drift:
        assert result.attributions == []
        return
    pred = result.predicted
    assert pred is not None
    assert pred.culprit_hop == f"h{chain.label.mutated_hop}"
    expected = {"STALE_CONTEXT": "STALE_CONTEXT", "INJECTED_INSTRUCTION": "INJECTION"}
    assert pred.cause == expected.get(chain.label.mutation_op, "REWRITE")
    for cid in chain.label.violated_constraints:
        a = result.for_constraint(cid)
        assert a is not None
        assert a.culprit_hop == pred.culprit_hop


async def test_rules_slice_reports_and_renders() -> None:
    rep = SliceReport()
    for chain in CHAINS[:24]:
        rep.add(await run_chain(chain, detector="rules", dwell_s=300.0))
    data = rep.as_dict()
    assert data["drifted"]["n"] + data["controls"]["n"] == 24
    md = render_markdown(
        {
            "benchmark": "generated", "split": "dev", "chains": 24,
            "drifted": data["drifted"]["n"], "generated_at": "now",
            "results": {"rules/default-dwell": {"dwell_threshold_s": 300.0, **data}},
        }
    )  # fmt: skip
    assert "rules/default-dwell" in md
