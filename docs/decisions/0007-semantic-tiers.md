# ADR 0007: Semantic tiers (Phase 5)

- **Status:** accepted
- **Date:** 2026-11-09
- **Spec reference:** ARCHITECTURE.md §6.3, §7.1, §7.4, §7.8, §9.3, §10, §11.1, §12.2, §17, §18, §19.4, §20, §21.1

## Context

Phase 5 delivers Tier 1b (lexical), Tier 1c (NLI), and Tier 2 (LLM judge with cache),
plus uncertainty-band routing. Exit criteria: per-tier latency within budget, and the
judge failure path degrades correctly. Several points are open in the spec.

## Decision

1. **Routing is shared state, not hard-coded.** `CheckContext.routing` carries
   `escalated` (constraints a tier passed on: the NLI band, an unavailable NLI model) and
   `reverified` (constraints a semantic tier scored as passing). Tier 2 decides
   `escalated` ∪ stale-and-not-yet-re-verified. An escalation that no tier settles (no
   Tier 2 configured, or skipped) makes the verdict degraded for that constraint, and
   the fail policy decides (§10.1). The checker stays a registry (§5 open/closed).
2. **Tier 1b reuses the extraction parser** (amounts, units, approval cues). It reports
   only what the words show: a loosening phrase ("may exceed", "no limit", "in full",
   "ignore previous limits"); a stated amount above the bound when the bound itself is
   absent; a bound missing with no generic reference to it ("within the limit"); and an
   approval requirement dropped or negated. A **stricter** stated cap is compatible: a
   payments agent writing the effective $50 under a $100 user limit is not drift. Any
   finding is terminal (§7.4 table). Tier 1b produces no scores, so it never counts as
   re-verification of a stale constraint, but a lexical finding on one counts as a
   *failed* re-verification (→ `STALE`).
3. **Tier 1b checks every registered constraint, declared policies included** (§7.6
   "as entailed by T_k"). The reference pipelines therefore put the policy sentence into
   the root task text the agents receive. The trace is still opened with the user's
   words; T_0 is not part of the envelope digest. A deployment whose agents never see a
   declared policy will get it re-grounded into their context, which is the §7.8 remedy.
4. **Tier 1c** scores T_k ⇒ `canonical_text(C)` per constraint in a worker thread under
   the 500 ms hard timeout (§10.3). Score ≥ θ_high passes, below θ_low is drift, the band
   escalates. A failure, a timeout or an out-of-range score escalates every constraint
   (§10.1 "NLI unavailable → Tier 2"). **Embedding routing** (§7.4 rule 4) only
   short-circuits a band score whose text is clearly unrelated (cosine below
   `CL_ROUTE_IRRELEVANT_BELOW`, default 0.15) to NOT_ENTAILED, saving a judge call. It
   never produces ENTAILED. A timed-out thread cannot be killed, but its result is
   discarded.
5. **Tier 2 evidence guard** (§21.1 "judge verdicts require a quoted evidence span").
   `JudgeResult` gains an optional `evidence`. The prompt asks for the exact span of the
   **task text** that carries the constraint, and Tier 2 accepts ENTAILED only when that
   span really occurs in T_k (case- and whitespace-insensitive). Otherwise it is
   downgraded to AMBIGUOUS, and the risk class decides (§7.4 rule 5). Quoting the
   constraint itself would prove nothing, because an attacker cannot change it. The
   anti-manipulation value comes from forcing the judge to point at the task text.
6. **Judge cache** (§7.4): key `sha256(canonical_text ‖ normalized(T_k) ‖ prompt_version ‖
   judge_model)`, normalized = whitespace collapsed and case-folded. It is a `JudgeCache`
   port with in-memory and SQLite adapters. The SQLite table `judge_cache` (§12.2) lives
   in the event-store database but is not a projection, so `project_all()` leaves it
   alone. The first answer wins. `CL_JUDGE_CACHE=false` disables it for evaluation runs.
7. **Replay never spends money.** Replay re-runs the live tiers with Tier 2 in
   cache-only mode: a cache miss is degraded, never a new judge call (§11.1 "no side
   effects"). A replay with no tier override uses exactly the live checker's tiers; it
   used to rebuild the *default* set, which would have reported spurious changes.
8. **Judge failures are visible.** Every failure (exception, timeout, refusal, output
   still invalid after one repair retry) becomes a `JUDGE_FAILED` event and a degraded
   verdict for that constraint. `/v1/metrics` counts them.
9. **Adapters.** `AnthropicJudge` uses the official SDK (optional extra `judge`,
   imported lazily). The versioned system prompt is cached (`cache_control`), the answer
   is constrained by structured output (`output_config.format`, JSON schema) at `low`
   effort, and the default model is `claude-opus-5-5` when `CL_JUDGE_MODEL` is empty.
   On models that support it, the server-side refusal fallback is enabled
   (`fallbacks="default"`, `CL_JUDGE_FALLBACKS`). A refusal that still stands is a judge
   failure, never a verdict. `OpenAICompatJudge` (plain httpx, JSON mode) serves
   self-hosted models. `CrossEncoderNLI` reads the entailment label index from the model
   config instead of assuming it, and `SentenceTransformerEmbedder` provides the routing
   embeddings; both are in the optional dependency group `models`. Prompts are loaded
   from `docs/judge_prompts/<name>_<version>.md` (or `CL_JUDGE_PROMPT_DIR`).
10. **Wiring.** `TierDeps` carries the models, judge, cache and thresholds. A tier whose
    dependency is missing is not registered, and `/v1/health` lists the live tiers and
    model status. Asking for an unavailable tier explicitly is a 400.
11. **SDK.** `guard_hop` now applies a CONSUME re-ground (the reminder is added before the
    agent acts), as `GuardedWorker` already did.

## Consequences

- Exit criterion 1: `eval/latency_budget.py` (report in `eval/reports/latency_budget_v1.md`)
  measures T0/T1a/T1b at p95 ≤ 0.1 ms against the 5 ms budget, and end to end without
  escalation (including the SQLite write) at about 12 ms against 150 ms. A unit test
  fails CI on a regression (§20). Tier 1c/2 run with fakes there, so their real latency
  depends on the models chosen at evaluation time (§18). It is bounded by the hard
  timeouts, which the tests enforce with slow fakes.
- Exit criterion 2: the judge failure path (raises, times out, invalid output twice,
  refusal) degrades per the fail policy: high-risk BLOCK, low-risk ALLOW_WITH_WARNING,
  with `JUDGE_FAILED` recorded. This is tested in units and end to end through the SDK.
- With Tier 1b on by default, the reference pipelines now stop drift **at the hop that
  introduced it** (re-ground, then block after `max_regrounds`), before any tool runs.
  That is the §2.1 / demo-script behaviour. QUALIFIER_DELETE is now caught too: it is
  real drift, even though the drifted action would have been harmless.
- **Known gaps, kept visible:** SCOPE_WIDEN on "refund this customer" (subject `*`)
  needs a real NLI model or judge; the lexical tier cannot see it. Thresholds θ are the
  §18 starting points until Phase 8 tunes them on the validation split. Real NLI and
  judge adapters are tested against stand-ins and an offline SDK transport. No live
  model ran here (no API key or model weights on the development host; live judge tests
  are opt-in with `CL_JUDGE_LIVE=1`). Trace state is re-folded per checkpoint and the
  ordering check walks hop ancestry, so cost grows with trace length. That is fine for
  realistic traces (tens of hops) and is a Phase 10 hardening item for very long ones.
