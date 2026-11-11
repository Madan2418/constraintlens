# ADR 0009: Drift attribution (Phase 7)

- **Status:** accepted
- **Date:** 2026-11-11
- **Spec reference:** ARCHITECTURE.md §4.2, §4.4, §7.7, §8.4, §9.3–§9.5, §11.1, §12.1, §14.3, §21.1, §23

## Context

Phase 7 delivers DAG attribution, async-cause classification and fan-in handling. Exit
criteria: attribution accuracy measured on `val`; the replay endpoint works. §7.7 gives
the algorithm (edge deltas, the first hop that crosses below threshold with a delta
above `δ_min`, the minimum-parent baseline at fan-in, async causes blamed on the queue
edge) and the output model. It leaves open what `E_k[c]` is when no semantic tier
scored a hop, how staleness is recognised, when attribution is recorded, and how a
violation is blamed when no text drifted.

## Decision

1. **A pure view over the log** (`domain/attribution.py`). `build_graph` folds the
   events into a hop DAG. Each checkpoint is read against the state *as it was when it
   arrived*, as replay does. `attribute` returns one `Attribution` (§7.7 step 5, model
   unchanged) per registered constraint that failed. Held and conflicting checks
   (ADR 0006) are not the checkpoint's answer, so they are ignored. Retries
   (`h2/r1`) and tool sub-hops (`h3.t1`) fold into their base hop.
2. **What `E_k[c]` is.** Every check of T_k contributes: the hop's PUBLISH and
   TOOL_CALL, and a downstream CONSUME of the same text. A semantic score (1c / 2) is
   used as is. A check that reported a drift failure without a score counts 0.0. A
   check that passed without a score counts 1.0, i.e. "no evidence of drift". `E_k` is
   the minimum, and `E_root = 1.0` (T_0 is what the constraints were issued from). With
   rules-only tiers the deltas are therefore {0, 1}. A hop is *failing* when a check
   reported a drift-kind failure (`TEXTUAL_DRIFT`, `CONSTRAINT_LOST`, `_DROPPED`,
   `HASH_MISMATCH`, `EXPIRED`, `STALE`) or `E_k < θ`. Ordering and gap findings
   (`OUT_OF_ORDER`, `MISSING_HOP`) say nothing about a constraint's meaning and are not
   used.
3. **Parameters.** `θ` defaults to `CL_THETA_LOW` (the NLI "not entailed" bound) and
   `δ_min` to `CL_DELTA_MIN` (0.15, already in §18). Both can be overridden per request
   (`GET .../attribution?theta=&delta_min=`, replay body). They are tuned on `val` in
   Phase 8, not here.
4. **Fan-in and propagation (§7.7 step 3).** The baseline is the minimum parent score.
   A failing hop whose drop does not exceed `δ_min` "matches its worst parent". Blame
   then walks up the worst-parent chain to the nearest hop whose own drop exceeds
   `δ_min`. If it reaches the root without finding one, the drift was gradual: the
   result is `culprit_hop = null`, `UNKNOWN`, rather than an arbitrary hop.
5. **Async causes (§7.7 step 4) are decided at CONSUME.** At hop *j*'s CONSUME of
   *p*'s envelope:
   - *Stale and not re-verified*, failed re-verification (`STALE`), or `EXPIRED`:
     `STALE_CONTEXT`. Staleness is recomputed with `async_guard.stale_constraints` at
     the checkpoint's receive time, using the configured guard. "Not re-verified" means
     no semantic tier gave the constraint a passing score. This is §4.2's definition:
     "exceeds its freshness bound without re-verification".
   - A carrier failure (lost, dropped, hash mismatch), or a failing check of text that
     differs from what *p* published: `TRANSIT_LOSS`.
   - A failing check of the *same* text *p* published is one more check of T_p and
     blames *p* (a rewrite the PUBLISH check missed, or a hop without a PUBLISH
     checkpoint).

   Edge culprits are named by the consuming hop with `culprit_agent = null`: "the queue
   edge, not an agent". A sweeper loss (§9.5 d) is `TRANSIT_LOSS` named by the
   publishing hop, again with no agent.
6. **INJECTION** is a cause, not a detector. The culprit hop's text must *introduce*
   an attributed note (`<Source> note|output|reply|message|quote…:`) that tries to
   override a limit ("ignore previous limits", "no longer applies", "full amount",
   "already cleared", "is compliant"), and no parent's text may already contain one.
   The patterns were written against the `dev` split. The marker never changes a
   decision.
7. **Violations without drift (§4.2).** If the constraint never failed in text but a
   TOOL_CALL violated it, the culprit is the acting hop with cause `UNKNOWN`: the
   action broke the constraint while every check said the text still entailed it. The
   exception is an ancestor that introduced an injected override (the adversarial
   notes that defeat Tier 1b), which is blamed with `INJECTION`.
8. **Recording (§7.7 "lazily on failure", §11.1 "close … triggers final
   attribution").**
   - `GET /v1/traces/{id}/attribution` computes the view on demand and writes nothing.
   - `CloseTrace` appends `ATTRIBUTION_COMPUTED` (attributions, parameters,
     `through_event_id`) in the same batch as `TRACE_CLOSED`, so the audit trail keeps
     what was concluded and under which parameters. It is written even when the list
     is empty: "nothing to attribute" is also a conclusion.
   - The projection (`TraceState.attributions`) and the trace summary expose the
     recorded result.
9. **Replay (§11.1, §14.4).** `ReplayItem` now carries each re-check's outcome. `POST
   .../replay` returns `attribution.original` (recorded verdicts) and
   `attribution.replayed` (the replayed verdicts, e.g. with a tier ablated), both under
   the same parameters. A replayed verdict is not an event, so its evidence cites only
   the checkpoint event.
10. **`OpenTraceRequest.extract = False`** registers only the declared specs. The
    benchmark harness needs this so extraction errors do not leak into attribution.
    The API body is unchanged.
11. **Measurement** (`eval/attribution_accuracy.py`, `make attribution-eval`):
    - **Replay.** Each `val` chain is replayed through the real use cases with a frozen
      clock that follows its consumption times. Every hop does CONSUME, then TOOL_CALL
      if it acts, then PUBLISH (as `guard_hop` does), and redeliveries are submitted
      twice. Every checkpoint is submitted whatever the verdict ("observe-only"),
      because the chain text is a recording.
    - **Scoring.** The predicted culprit is the earliest hop named. It is correct when
      it equals `label.mutated_hop` (§4.4).
    - **Detectors.** `rules` (T0, T1a, T1b) is the system result. `oracle-text` swaps
      T1b for the benchmark's own reference features: an exact text detector that
      isolates the algorithm. It is an upper bound, not a system result.
    - **Dwell slices.** The 300 s default dwell threshold, and an "age-only" slice (dwell
      threshold = the 6 h age bound). Without a semantic tier, the 300 s default turns
      every long queue edge into an unverifiable stale constraint.

## Results on `val` (816 chains, 408 drifted)

| Detector / slice | Hop accuracy | If flagged | Agent | Cause (hop correct) | Controls with any attribution |
|---|---|---|---|---|---|
| rules, dwell 300 s (defaults) | **70.8%** | 71.9% | 55.6% | 75.1% | 74.5% |
| rules, age-only | **85.0%** | 88.5% | 85.0% | 94.5% | 27.9% |
| oracle-text, dwell 300 s | 81.1% | 81.1% | 65.4% | 80.7% | 63.7% |
| oracle-text, age-only | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% |

Full tables (per operator, per topology): `eval/reports/attribution_val_v1.md`.

## Consequences

- **Exit criteria met:**
  - **Attribution accuracy is measured on `val` and committed.** Given exact detection,
    the algorithm names the labeled hop and cause on every drifted chain, and attributes
    nothing on controls (also pinned by a test on generated chains).
  - **Replay works** and now reports replayed attribution.
- **The system number is bounded by detection.**
  - With rules only, SCOPE_WIDEN (34% hop accuracy, age-only) and NEGATION_FLIP (72%)
    lose most. Tier 1b does not read "every customer in the segment" or "a soft target
    only" as drift, so the violation fallback names the acting hop with `UNKNOWN`.
  - The 114 control attributions in the age-only slice are Tier 1b false positives on
    hard negatives (`REWRITE`).
  - These gaps belong to the semantic tiers. No NLI model or judge ran on this host, so
    the configurations of §14.2 that use them are still to be measured. Tier 1b was not
    tuned to the benchmark.
- **The dwell threshold dominates the rules-only result.** At 300 s, 74.5% of controls
  get a `STALE_CONTEXT` attribution. Nothing re-verifies their constraints, so §4.2
  counts them as stale failures, and the fail-closed policy already blocked them.
  Choosing the threshold is Phase 8 work on `val`. The two slices are reported so that
  choice is informed, and neither is chosen here.
- **Pending from Phase 6:** the benchmark labels are still not hand-validated, so these
  accuracies are against generator labels.
- New code: `domain/attribution.py`, `application/attribute_trace.py`, the endpoint,
  `ATTRIBUTION_COMPUTED` at close, attribution in replay, and the attribution card in
  the protected pipelines' console reports (demo step 3).
