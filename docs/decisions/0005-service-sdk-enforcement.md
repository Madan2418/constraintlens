# ADR 0005: Service, SDK, Tier 0/1a and enforcement (Phase 3)

- **Status:** accepted
- **Date:** 2026-11-07
- **Spec reference:** ARCHITECTURE.md §6.4, §7.2–7.4, §7.8, §7.9, §9.6, §10, §11, §12, §21.1

## Context

Phase 3 delivers the FastAPI service, the SDK, the SQLite event store, Tiers 0 and 1a,
enforcement, and direct-handoff checkpoints. Several points are open or in tension in
the spec.

## Decision

1. **Tool bindings.** Tier 1a needs to know which tool argument is the amount, unit and
   subject. `ProposedAction` gains an optional `binding: ActionBinding` (action, resource,
   `amount_arg`, `unit` | `unit_arg`, `subject_arg`, `subject_kind`). The pipeline author
   declares it once per tool in `guard_tool`. A tool without a binding is not checked by
   Tier 1a. An unreadable bound argument fails safe as an `ACTION_VIOLATION`.
2. **Approval breaches: BLOCK vs ESCALATE.** §2.1 shows BLOCK for "$2,000 against $100,
   approval above". §7.8 defines ESCALATE for "authority requires approval". Resolution:
   an approval-threshold breach is ESCALATE only when an approval route is configured
   (`CL_ESCALATE_APPROVALS=true`), and BLOCK otherwise. Delivering to a webhook is not
   built yet; ESCALATE blocks either way. Strictness order: ALLOW < WARN < RE_GROUND <
   ESCALATE < BLOCK.
3. **Rows missing from the §7.8 table**: EXPIRED and UNSATISFIABLE → BLOCK; MISSING_HOP →
   BLOCK (high) / WARN (low), per §9.4; OUT_OF_ORDER → WARN ("checked, flagged", §9.4).
   A RE_GROUND that names no registered constraint has nothing to re-inject → BLOCK.
4. **One tool call per hop.** §4.1 says a hop emits one delegation *or* one tool call, and
   verdicts are keyed by `(trace, hop, checkpoint)`. So each guarded tool call gets a
   sub-hop id `<hop>.t<n>`. A re-executed agent re-issues the same ids, so redelivery
   stays idempotent.
5. **Re-ground retries** get their own hop id, `<hop>/r<n>`. Otherwise the retry would be
   answered as a duplicate (§9.6) and would break `UNIQUE(trace_id, hop_id, checkpoint)`
   (§12.2). `max_regrounds` counts across all attempts of the base hop.
6. **Idempotency.** Lookup is by client `Idempotency-Key` *or* by `(trace, hop,
   checkpoint)`. A new client key for an already-verified checkpoint returns the stored
   verdict instead of violating the UNIQUE index. Concurrent duplicates wait on a per-key
   lock (§9.6).
7. **Checkpoint responses are 200 with the verdict**, BLOCK included (§11.2 contract).
   `ConstraintViolation` is raised by the SDK at the gate. Errors use RFC 7807; request
   validation failures are 400 `InvalidEnvelope` (§11.4).
8. **Recorder failure** (§7.9): the verdict is still returned but marked `degraded`.
   Its decision is raised to BLOCK when any constraint is high-risk and the policy is
   closed, else to at least ALLOW_WITH_WARNING.
9. **Replay** re-runs checker and enforcement over stored `CHECKPOINT_RECEIVED` requests,
   at their original receive time, with optional tier/policy overrides, and writes
   nothing. Task text is stored redacted unless `CL_STORE_FULL_TEXT`. Tiers 0 and 1a don't
   read the text, so replay is exact for them. Semantic tiers (Phase 5) replay over
   redacted text, a stated limitation.
10. **Trace state is a fold of events** (`domain/projections.py`) used by every use case.
    The SQLite adapter keeps SQL projections in the same transaction and can rebuild them
    (`project_all`); a test proves the rebuild is identical.
11. **API protection**: optional `X-API-Key` (`CL_API_KEY`, constant-time compare; health
    stays open for probes) and a request size limit (`CL_MAX_REQUEST_BYTES`). Rate
    limiting is not built (Phase 10 hardening).
12. **Layout**: wiring lives in `api/wiring.py` (the composition root) so routes can use it
    without import cycles; `api/app.py` is the FastAPI factory. The LangGraph adapter is
    `adapters/pipeline/langgraph.py` (envelope in graph state); the generic `guard_hop` is
    in the SDK. A new import contract keeps server code out of the SDK.
13. **Extraction in the service runs rules-only** until a real judge adapter exists
    (Phase 5); `/v1/health` reports this.

## Consequences

- Phase 3 exit criteria are tested end to end (`tests/e2e/test_protected_pipeline.py`).
  Every amount-based violation is blocked at the tool gate. The event log holds a verdict
  for every checkpoint, replay reproduces all decisions, and projections rebuild
  identically.
- **Known gap, kept visible**: SCOPE_WIDEN is not caught. "Refund this customer" names no
  order, so the extracted subject is `*`, and refunding another order of the same
  customer is structurally in scope. Catching it needs the semantic tiers (Phase 5) or a
  subject supplied at trace open.
