# Attribution accuracy on driftchain-bench `val` (Phase 7)

Benchmark `driftchain-bench-v0.1.0-seed7-n4000.jsonl`, split `val`: 816 chains (408 drifted, 408 controls). Generated 2026-11-11T06:04:32+00:00.

Correct = the earliest culprit hop named equals `label.mutated_hop` (§4.4). `rules` is the system with the tiers available on this host (T0, T1a, T1b; no NLI model, no judge). `oracle-text` swaps T1b for the benchmark's reference features, an exact text detector, to isolate the attribution algorithm: an upper bound, not a system result. theta = 0.35, delta_min = 0.15 (defaults, untuned).

## Summary

| Detector / slice | dwell threshold | flagged | hop acc. | hop acc. if flagged | agent acc. | cause acc. (hop correct) | violated-constraint hop acc. | controls with any attribution |
|---|---|---|---|---|---|---|---|---|
| `rules/default-dwell` | 300 s | 98.5% | **70.8%** | 71.9% | 55.6% | 75.1% | 69.5% | 74.5% |
| `rules/age-only` | 21600 s | 96.1% | **85.0%** | 88.5% | 85.0% | 94.5% | 87.6% | 27.9% |
| `oracle-text/default-dwell` | 300 s | 100.0% | **81.1%** | 81.1% | 65.4% | 80.7% | 79.7% | 63.7% |
| `oracle-text/age-only` | 21600 s | 100.0% | **100.0%** | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% |

## `rules/default-dwell` by operator

| Operator | n | flagged | hop acc. | hop acc. if flagged | cause acc. |
|---|---|---|---|---|---|
| CONSTRAINT_DROP_ONE | 54 | 100.0% | 79.6% | 79.6% | 86.1% |
| INJECTED_INSTRUCTION | 51 | 100.0% | 74.5% | 74.5% | 68.4% |
| NEGATION_FLIP | 46 | 91.3% | 63.0% | 69.0% | 75.9% |
| NUMERIC_LOOSEN | 57 | 100.0% | 66.7% | 66.7% | 71.0% |
| QUALIFIER_DELETE | 46 | 100.0% | 76.1% | 76.1% | 85.7% |
| SCOPE_WIDEN | 53 | 96.2% | 37.7% | 39.2% | 0.0% |
| STALE_CONTEXT | 49 | 100.0% | 87.8% | 87.8% | 100.0% |
| SUMMARIZATION_LOSS | 52 | 100.0% | 82.7% | 82.7% | 74.4% |

| Topology | n | hop acc. | hop acc. if flagged |
|---|---|---|---|
| fan_in | 110 | 68.2% | 68.2% |
| fan_out | 84 | 61.9% | 63.4% |
| linear | 214 | 75.7% | 77.1% |

Attributions on control chains, by cause: REWRITE 74, STALE_CONTEXT 379.

## `rules/age-only` by operator

| Operator | n | flagged | hop acc. | hop acc. if flagged | cause acc. |
|---|---|---|---|---|---|
| CONSTRAINT_DROP_ONE | 54 | 100.0% | 98.2% | 98.2% | 100.0% |
| INJECTED_INSTRUCTION | 51 | 98.0% | 90.2% | 92.0% | 100.0% |
| NEGATION_FLIP | 46 | 80.4% | 71.7% | 89.2% | 97.0% |
| NUMERIC_LOOSEN | 57 | 100.0% | 100.0% | 100.0% | 100.0% |
| QUALIFIER_DELETE | 46 | 100.0% | 93.5% | 93.5% | 100.0% |
| SCOPE_WIDEN | 53 | 88.7% | 34.0% | 38.3% | 0.0% |
| STALE_CONTEXT | 49 | 100.0% | 93.9% | 93.9% | 100.0% |
| SUMMARIZATION_LOSS | 52 | 100.0% | 98.1% | 98.1% | 100.0% |

| Topology | n | hop acc. | hop acc. if flagged |
|---|---|---|---|
| fan_in | 110 | 82.7% | 87.5% |
| fan_out | 84 | 79.8% | 82.7% |
| linear | 214 | 88.3% | 91.3% |

Attributions on control chains, by cause: REWRITE 114.

## `oracle-text/default-dwell` by operator

| Operator | n | flagged | hop acc. | hop acc. if flagged | cause acc. |
|---|---|---|---|---|---|
| CONSTRAINT_DROP_ONE | 54 | 100.0% | 79.6% | 79.6% | 86.1% |
| INJECTED_INSTRUCTION | 51 | 100.0% | 84.3% | 84.3% | 69.8% |
| NEGATION_FLIP | 46 | 100.0% | 80.4% | 80.4% | 83.8% |
| NUMERIC_LOOSEN | 57 | 100.0% | 68.4% | 68.4% | 71.8% |
| QUALIFIER_DELETE | 46 | 100.0% | 80.4% | 80.4% | 83.8% |
| SCOPE_WIDEN | 53 | 100.0% | 79.2% | 79.2% | 73.8% |
| STALE_CONTEXT | 49 | 100.0% | 93.9% | 93.9% | 100.0% |
| SUMMARIZATION_LOSS | 52 | 100.0% | 84.6% | 84.6% | 75.0% |

| Topology | n | hop acc. | hop acc. if flagged |
|---|---|---|---|
| fan_in | 110 | 77.3% | 77.3% |
| fan_out | 84 | 76.2% | 76.2% |
| linear | 214 | 85.0% | 85.0% |

Attributions on control chains, by cause: STALE_CONTEXT 409.

## `oracle-text/age-only` by operator

| Operator | n | flagged | hop acc. | hop acc. if flagged | cause acc. |
|---|---|---|---|---|---|
| CONSTRAINT_DROP_ONE | 54 | 100.0% | 100.0% | 100.0% | 100.0% |
| INJECTED_INSTRUCTION | 51 | 100.0% | 100.0% | 100.0% | 100.0% |
| NEGATION_FLIP | 46 | 100.0% | 100.0% | 100.0% | 100.0% |
| NUMERIC_LOOSEN | 57 | 100.0% | 100.0% | 100.0% | 100.0% |
| QUALIFIER_DELETE | 46 | 100.0% | 100.0% | 100.0% | 100.0% |
| SCOPE_WIDEN | 53 | 100.0% | 100.0% | 100.0% | 100.0% |
| STALE_CONTEXT | 49 | 100.0% | 100.0% | 100.0% | 100.0% |
| SUMMARIZATION_LOSS | 52 | 100.0% | 100.0% | 100.0% | 100.0% |

| Topology | n | hop acc. | hop acc. if flagged |
|---|---|---|---|
| fan_in | 110 | 100.0% | 100.0% |
| fan_out | 84 | 100.0% | 100.0% |
| linear | 214 | 100.0% | 100.0% |

Attributions on control chains, by cause: none.
