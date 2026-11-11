# ADR 0010: Evaluation harness (Phase 8)

- **Status:** accepted (harness); results are **provisional** (val only)
- **Date:** 2026-11-11
- **Spec reference:** ARCHITECTURE.md §4.4, §9.4, §9.6, §10.1, §12.2, §13.7, §14, §19.4, §23

## Context

Phase 8 delivers threshold tuning on `val`, every §14.2 configuration on `test`,
ablations, async slices, live-pipeline trials and statistics. The exit criterion is
"all tables and figures reproducible via `make eval`". Three inputs are missing on this
host:

1. **Human validation labels.** ADR 0008 says tuning waits for them.
2. **An NLI model.** torch is not installed.
3. **A judge API key.**

The protocol touches `test` once per configuration, after thresholds are frozen
(§14.4). Running it now would spend that one look on a system without its semantic
tiers.

**Decision taken with the user:** build the whole harness, run everything runnable on
`val`, do a provisional threshold sweep, and keep `test` locked.

## Decision

1. **One runner** (`eval/runner.py`).
   - **Replay.** A chain is replayed through the real use cases under a configuration,
     with a frozen service clock that follows the chain and a real monotonic clock for
     tier latency.
   - **Checkpoints.** Each hop does CONSUME, then TOOL_CALL, then PUBLISH (like
     `guard_hop`), and redeliveries are submitted twice.
   - **Constraints** are registered from the record specs (`extract=False`).
   - **Shared.** The Phase 7 attribution harness now uses the same runner; its results
     are byte-identical.
2. **Configurations** (`eval/configs`):
   - **Baselines.** B0 (nothing); B1 (Tier 0 integrity only); B2 (one text check at
     hop 1, using Tier 1b because Tier 1c is absent); B3 (Tier 1a at the tool call
     only).
   - **System configurations.** C1, C2, the model-dependent C3 ablations, and EMB
     declare the models they need. When a model is missing they are reported as **not
     run, with the reason**, never substituted.
   - **Two additions, labeled everywhere:**
     - `R` is ConstraintLens with the rules tiers. It is C1 without Tier 1c, the
       system that actually runs without an NLI model. The model-free ablations
       (`C3-T1b`, `C3-guard`, `C3-comp`) are taken from it.
     - `ORACLE` uses the benchmark's reference features as an exact text detector. It
       is an analysis upper bound.
   - **Ablation switches.** `GuardConfig.enabled` (new, default on) turns the async
     guard off. Composition is ablated by registering only the user's constraint.
3. **Metrics** (`eval/metrics.py`). The spec's wording is made exact in the module
   docstring:
   - **Flag.** A constraint failure, a blocking decision, or a degraded warning.
   - **Detected.** A flag at the mutated hop or a descendant, before the violating
     tool runs (§4.4).
   - **Halted.** A BLOCK or ESCALATE upstream, or a non-allowing tool-call decision.
     A RE_GROUND upstream does not count: a recording cannot retry.
   - **Derived metrics:**
     - violation prevention;
     - false block (any control-chain action halted);
     - precision and F1 (FP = a control chain with any flag);
     - lag;
     - attribution;
     - composition recall (the dropped org constraint named);
     - redelivery conflicts;
     - degraded rate;
     - per-tier latency.
   - **Breakdowns.** Per operator (mandatory, §14.3), per topology, and per async
     slice (dwell, reorder, redelivery, fan-in; RQ5).
4. **Statistics** (`eval/stats.py`). Percentile bootstrap over **chains** (a chain's
   checkpoints are not independent), with 1,000 resamples and seed 7.
5. **Provisional tuning** (`eval/tune.py`).
   - **What is swept.** Only the dwell threshold moves results on this host. With no
     semantic scores, θ_low, θ_high and δ_min have no effect and stay at their
     defaults, listed as untuned.
   - **Objective, fixed before the sweep:** maximize detection recall subject to
     false-block ≤ 5% on `val` controls. Ties go to the lower false-block rate, then to
     the smaller threshold.
   - **Output.** `eval/configs/thresholds_provisional.json` with
     `"status": "provisional"`.
6. **The test split is locked.**
   - `eval.run --split test` refuses unless `eval/configs/thresholds.json` exists with
     `"status": "frozen"`.
   - Every test run is appended to `eval/reports/test_ledger.json`, and a
     configuration already in it is refused.
   - Freezing is a deliberate manual step, after the labels and the semantic tiers.
7. **Reports.** `make eval` runs tuning, the `val` evaluation and the attribution
   evaluation. It writes Markdown, JSON and dependency-free SVG figures (one metric per
   figure, one bar per configuration, 95% CI whiskers, light and dark) to
   `eval/reports/`. `make eval-test` is the locked test run.
8. **A real bug found and fixed: fan-in joins.**
   - **The bug.** A join hop consumes one message per parent. With §9.6's key
     `(trace, hop, checkpoint)`, its second CONSUME collided with the first and was
     treated as a conflicting duplicate. It never became final, and every redelivery
     of it got a fresh verdict: 17 conflicting redeliveries on `val`, in every
     configuration that checks CONSUME. The Phase 4 tests missed it because every
     pipeline is linear.
   - **The fix.** The default key of a CONSUME now also names the consumed envelope's
     hop: `trace:hop:CONSUME<parent`. A linear hop still has exactly one CONSUME, so
     only the key string changes for it.
   - **Where it lands:**
     - `VERDICT_ISSUED` records `consumed_hop_id`;
     - the SQLite `verdicts` projection gains a `consumed_hop_id` column inside its
       UNIQUE constraint (a refinement of the §12.2 schema; databases created earlier
       need their projections rebuilt);
     - the projections use the same key.
   - **Verification.** A regression test on both stores fails on the old code. After
     the fix there are 0 conflicting redeliveries.
9. **The RunEvaluation service endpoints** (`POST /v1/eval/runs`, §11.1) are not built
   here. They would need the service to read the benchmark, which an import-linter
   contract forbids (labels never reach ConstraintLens). The CLI (Phase 9) is the entry
   point. The endpoint would have to run the evaluation as a separate process.

## Results on `val` (816 chains, 408 drifted; provisional dwell threshold 14,400 s)

| Config | Detection recall | Violation prevention | False-block rate | F1 | Attribution |
|---|---|---|---|---|---|
| B0 no check | 0.0% | 0.0% | 0.0% | – | 0.0% |
| B1 presence-only | 0.0% | 0.0% | 0.0% | – | 0.0% |
| B2 one-shot gate (T1b) | 20.3% [16.5, 24.4] | 0.0% | 0.0% | 31.5% | 20.3% |
| B3 tool-arg only | 55.9% [50.9, 60.5] | 82.3% [77.7, 86.4] | 0.0% | 71.7% | 18.6% |
| **R** rules tiers | **96.1%** [94.1, 97.8] | **96.4%** [93.9, 98.6] | **8.6%** [5.9, 11.4] | 85.8% | 85.0% |
| C3-T1b (R − lexical) | 67.9% | 96.0% | 0.0% | 80.9% | 38.0% |
| C3-guard (R − async guard) | 86.3% | 83.8% | 8.6% | 80.5% | 75.0% |
| C3-comp (R − composition) | 84.1% | 78.7% | 8.6% | 79.3% | 72.5% |
| ORACLE (analysis bound) | 100.0% | 96.0% | 0.0% | 100.0% | 100.0% |

Not run: C1, C2, C3-T1c, C3-T2 (need an NLI model and/or a judge) and EMB (needs an
embedding model). Full tables, async slices, latency and figures are in
`eval/reports/eval_val_v1.md` and `eval/reports/figures/`. The sweep is in
`eval/reports/tuning_val_v1.md`.

## Consequences

- **What the numbers say.**
  - **R against the baselines.** Each R component carries weight:
    - Tier 1b: +28 points of recall over C3-T1b;
    - the async guard: STALE_CONTEXT recall 100% vs 18%, and +13 points of
      prevention;
    - composition: 100% vs 9% on CONSTRAINT_DROP_ONE, and +18 points of prevention.
  - **R against B3.** R also beats the tool-arg-only baseline B3: +40 points recall
    and +14 points prevention.
  - **The ORACLE gap** shows where detection, not the architecture, limits the rules
    system:
    - SCOPE_WIDEN (88.7%) and NEGATION_FLIP (80.4%) for recall;
    - the 8.6% false blocks: Tier 1b firing on hard-negative rewordings at high-risk
      tool calls.
- **The false-block bound (5%) is not met.** Without a semantic tier to re-verify, a
  stale high-risk constraint fails closed, so short dwell thresholds block most
  long-queue controls (56% at 300 s). Even with the dwell check effectively off, Tier 1b
  false positives leave 8.6%. The sweep therefore picked the lowest-false-block value,
  and the file records that the bound was missed. Both effects are what Tier 1c / 2
  exist to fix. This is a measured argument for them, not a tuned-away problem.
- **Low-risk stale constraints fail open (§10.1).** They are detected (a degraded
  warning) but not prevented, which is the policy working as specified.
- **Phase 8 exit criterion:** met for everything runnable. `make eval` reproduces
  every table and figure from the seed-7 benchmark. **Not done, and kept visible:**
  - `test` numbers;
  - C1, C2 and the model-dependent ablations;
  - EMB;
  - judge usage and cost;
  - live-pipeline trials with real LLM agents (needs an agent model key);
  - the human-label agreement (ADR 0008).

  Finishing Phase 8 means: collect the labels, install an NLI model and set a judge
  key, re-tune on `val`, write `thresholds.json` with `"status": "frozen"`, then run
  `make eval-test` once.
