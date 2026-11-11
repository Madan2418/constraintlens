# Evaluation on driftchain-bench `val` (Phase 8)

Benchmark `driftchain-bench-v0.1.0-seed7-n4000.jsonl`: 816 chains (408 drifted). Thresholds: **provisional** (dwell 14400 s, θ_low 0.35, θ_high 0.8, δ_min 0.15). 95% bootstrap intervals over chains (1000 resamples, seed 7). Generated 2026-11-11T06:55:19+00:00.

Definitions: `eval/metrics.py` and ADR 0010. `R` is ConstraintLens with the rules tiers only (C1 without Tier 1c). `ORACLE` is an analysis upper bound, not a system.

## Headline (§14.3)

| Config | Detection recall | Violation prevention | False-block rate | Precision | F1 | Lag (hops) | Attribution | Composition recall |
|---|---|---|---|---|---|---|---|---|
| `B0` No check | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | – | – | – | 0.0% | 0.0% |
| `B1` Presence-only | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | – | – | – | 0.0% | 0.0% |
| `B2` One-shot gate | 20.3% [16.5, 24.4] | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | 69.8% | 31.5% | 0.00 | 20.3% | 42.6% |
| `B3` Tool-arg check only | 55.9% [50.9, 60.5] | 82.3% [77.7, 86.4] | 0.0% [0.0, 0.0] | 100.0% | 71.7% | 1.32 | 18.6% | 94.4% |
| `R` ConstraintLens, rules tiers | 96.1% [94.1, 97.8] | 96.4% [93.9, 98.6] | 8.6% [5.9, 11.4] | 77.5% | 85.8% | 0.22 | 85.0% | 100.0% |
| `C1` ConstraintLens, no Tier 2 | not run: needs nli | | | | | | | |
| `C2` ConstraintLens, full | not run: needs nli, judge | | | | | | | |
| `C3-T1b` Ablation: no lexical tier | 67.9% [63.3, 72.4] | 96.0% [93.5, 98.3] | 0.0% [0.0, 0.0] | 100.0% | 80.9% | 1.09 | 38.0% | 94.4% |
| `C3-guard` Ablation: no async guard | 86.3% [82.8, 89.4] | 83.8% [79.3, 87.7] | 8.6% [5.9, 11.4] | 75.5% | 80.5% | 0.26 | 75.0% | 100.0% |
| `C3-comp` Ablation: no composition | 84.1% [80.6, 87.6] | 78.7% [73.8, 83.6] | 8.6% [5.9, 11.4] | 75.0% | 79.3% | 0.27 | 72.5% | 0.0% |
| `C3-T1c` Ablation: no NLI | not run: needs judge | | | | | | | |
| `C3-T2` Ablation: no judge | not run: needs nli | | | | | | | |
| `EMB` Embedding similarity only | not run: needs embedder | | | | | | | |
| `ORACLE` Oracle text detector (analysis) | 100.0% [100.0, 100.0] | 96.0% [93.5, 98.3] | 0.0% [0.0, 0.0] | 100.0% | 100.0% | 0.00 | 100.0% | 100.0% |

## Detection recall by operator (mandatory per-operator breakdown, §14.3)

| Operator | `B0` | `B1` | `B2` | `B3` | `R` | `C3-T1b` | `C3-guard` | `C3-comp` | `ORACLE` |
|---|---|---|---|---|---|---|---|---|---|
| CONSTRAINT_DROP_ONE | 0.0% | 0.0% | 42.6% | 94.4% | 100.0% | 94.4% | 100.0% | 9.3% | 100.0% |
| INJECTED_INSTRUCTION | 0.0% | 0.0% | 11.8% | 84.3% | 98.0% | 84.3% | 98.0% | 98.0% | 100.0% |
| NEGATION_FLIP | 0.0% | 0.0% | 19.6% | 32.6% | 80.4% | 32.6% | 80.4% | 80.4% | 100.0% |
| NONE (false-block rate) | 0.0% | 0.0% | 0.0% | 0.0% | 8.6% | 0.0% | 8.6% | 8.6% | 0.0% |
| NUMERIC_LOOSEN | 0.0% | 0.0% | 29.8% | 47.4% | 100.0% | 47.4% | 100.0% | 100.0% | 100.0% |
| QUALIFIER_DELETE | 0.0% | 0.0% | 34.8% | 0.0% | 100.0% | 0.0% | 100.0% | 100.0% | 100.0% |
| SCOPE_WIDEN | 0.0% | 0.0% | 0.0% | 88.7% | 88.7% | 88.7% | 88.7% | 88.7% | 100.0% |
| STALE_CONTEXT | 0.0% | 0.0% | 0.0% | 0.0% | 100.0% | 100.0% | 18.4% | 100.0% | 100.0% |
| SUMMARIZATION_LOSS | 0.0% | 0.0% | 23.1% | 86.5% | 100.0% | 86.5% | 100.0% | 100.0% | 100.0% |

## Async slices (RQ5): detection recall / false-block rate

| Slice | `B0` | `B1` | `B2` | `B3` | `R` | `C3-T1b` | `C3-guard` | `C3-comp` | `ORACLE` |
|---|---|---|---|---|---|---|---|---|---|
| dwell = hours | 0.0% / 0.0% | 0.0% / 0.0% | 18.8% / 0.0% | 35.9% / 0.0% | 98.3% / 3.9% | 77.8% / 0.0% | 64.1% / 3.9% | 88.0% / 3.9% | 100.0% / 0.0% |
| dwell = immediate | 0.0% / 0.0% | 0.0% / 0.0% | 22.0% / 0.0% | 70.9% / 0.0% | 96.5% / 10.8% | 70.9% / 0.0% | 96.5% / 10.8% | 83.7% / 10.8% | 100.0% / 0.0% |
| dwell = minutes | 0.0% / 0.0% | 0.0% / 0.0% | 20.0% / 0.0% | 57.3% / 0.0% | 94.0% / 8.8% | 57.3% / 0.0% | 94.0% / 8.8% | 81.3% / 8.8% | 100.0% / 0.0% |
| reorder = False | 0.0% / 0.0% | 0.0% / 0.0% | 21.3% / 0.0% | 58.4% / 0.0% | 96.5% / 8.5% | 70.0% / 0.0% | 86.8% / 8.5% | 83.9% / 8.5% | 100.0% / 0.0% |
| reorder = True | 0.0% / 0.0% | 0.0% / 0.0% | 17.3% / 0.0% | 48.0% / 0.0% | 94.9% / 8.7% | 61.2% / 0.0% | 84.7% / 8.7% | 84.7% / 8.7% | 100.0% / 0.0% |
| redelivery = False | 0.0% / 0.0% | 0.0% / 0.0% | 19.8% / 0.0% | 55.4% / 0.0% | 96.2% / 8.0% | 67.0% / 0.0% | 87.4% / 8.0% | 85.2% / 8.0% | 100.0% / 0.0% |
| redelivery = True | 0.0% / 0.0% | 0.0% / 0.0% | 22.2% / 0.0% | 57.8% / 0.0% | 95.6% / 10.5% | 71.1% / 0.0% | 82.2% / 10.5% | 80.0% / 10.5% | 100.0% / 0.0% |
| fan_in = False | 0.0% / 0.0% | 0.0% / 0.0% | 21.8% / 0.0% | 60.4% / 0.0% | 96.6% / 9.6% | 70.8% / 0.0% | 87.6% / 9.6% | 82.9% / 9.6% | 100.0% / 0.0% |
| fan_in = True | 0.0% / 0.0% | 0.0% / 0.0% | 16.4% / 0.0% | 43.6% / 0.0% | 94.5% / 5.6% | 60.0% / 0.0% | 82.7% / 5.6% | 87.3% / 5.6% | 100.0% / 0.0% |

Redelivered checkpoints that got a different verdict than the first delivery: `B0` 0, `B1` 0, `B2` 0, `B3` 0, `R` 0, `C3-T1b` 0, `C3-guard` 0, `C3-comp` 0, `ORACLE` 0.

## Overhead (RQ7) and degraded behavior

| Config | checks | degraded checks | end-to-end p50 / p95 / p99 (ms) |
|---|---|---|---|
| `B0` | 0 | – | – |
| `B1` | 7375 | 0.0% | 0.154 / 0.342 / 0.522 |
| `B2` | 1675 | 0.0% | 0.319 / 0.649 / 0.980 |
| `B3` | 816 | 0.0% | 0.082 / 0.199 / 0.303 |
| `R` | 7375 | 1.5% | 0.341 / 0.745 / 1.105 |
| `C3-T1b` | 7375 | 1.6% | 0.188 / 0.476 / 0.770 |
| `C3-guard` | 7375 | 0.0% | 0.333 / 0.866 / 1.403 |
| `C3-comp` | 7375 | 1.5% | 0.245 / 0.580 / 0.961 |
| `ORACLE` | 7375 | 1.6% | 0.235 / 0.567 / 0.871 |

Latency is checker time per checkpoint on this host (in-process store); the service end-to-end budget is measured by `make latency`. Judge usage, cache hit rate and cost per 1,000 checks need Tier 2, which did not run.

## Not yet run

- Configurations needing an NLI model or a judge (listed above).
- The `test` split: locked until thresholds are frozen (human validation labels pending, ADR 0008).
- Live-pipeline trials with real LLM agents (§14.4 step 6): need an agent model key.
