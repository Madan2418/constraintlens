# ADR 0006: Async boundary (Phase 4)

- **Status:** accepted
- **Date:** 2026-11-09
- **Spec reference:** ARCHITECTURE.md §6.4, §7.3, §7.5, §7.8, §9, §10, §11.1, §12, §20, §23

## Context

Phase 4 delivers the Redis Streams transport, the async guard (staleness, reordering,
loss, redelivery), idempotent verdicts at the queue boundary, the `PUBLISH` / `CONSUME`
checkpoints for queue-borne hops, and the Redis-worker adapter. Exit criteria: property
tests for ordering and idempotency pass; the failure-injection suite passes. Several
points are open in the spec, or in tension with ADR 0005.

## Decision

1. **The async guard runs inside Tier 0.** §7.4 lists "hop_seq valid" and `OUT_OF_ORDER`
   under Tier 0, and the `TierId` set is closed. `domain/async_guard.assess` is pure; it
   reasons over `HopHistory`, a projection of the recorded checkpoints, so live checks
   and replay see the same history. Integrity failures stay terminal. `OUT_OF_ORDER` and a
   released gap are *non-terminal*: the checkpoint is still checked by later tiers
   ("checked, not trusted", §9.4). Findings are now recorded against the tier that
   produced them (`TIER_EXECUTED.failures`), not all against the last tier.
2. **Gaps hold; the hold is not a final verdict** (§9.4 "block until the gap is resolved
   or a timeout elapses"). A `CONSUME` whose envelope names a never-observed parent, or
   whose `hop_seq` skips past its parents', gets a *held* verdict when any constraint is
   high-risk: `BLOCK`, `held=true` (a new `Verdict` field), a `MISSING_HOP` failure. It
   is recorded (`CHECKPOINT_RECEIVED{held}`, `TIER_EXECUTED`, a `WARNING{held, verdict}`,
   `DECISION_ENFORCED{final:false}`) but is **not** stored as the idempotent answer, so it
   does not touch `UNIQUE(trace, hop, checkpoint)`. The consumer does not acknowledge the
   message; its redelivery is checked again. Once the predecessor is observed it
   proceeds. Once `CL_JOIN_TIMEOUT_S` has passed since the *first hold* (service clock,
   never the client's `emitted_at`, which a producer could backdate), it proceeds with a
   `MISSING_HOP` warning. Low risk never holds. This supersedes ADR 0005 item 3 for
   `MISSING_HOP`: the decision-table row is now `ALLOW_WITH_WARNING` for what proceeds,
   and the hold itself is the block.
3. **The root hop is known from `TRACE_OPENED`.** The service issues it, so it is
   observed from the start. Without this, a reordered consume of the root's child was
   held for no reason; the ordering property test found it.
4. **Staleness demands a semantic tier** (§9.3). Stale constraints are computed once per
   checkpoint (`CheckContext.stale`). After the tiers run, each one is re-verified (a tier
   with `semantic = True`, Tier 1c or 2, scored it), failed (that tier reported a finding
   on it → a `STALE` failure, §7.8 row "re-verification fails"), or unverifiable (→
   `degraded` for that constraint, and the fail policy decides, §10.1). Tier 1b is
   lexical and does not count. **Until Phase 5 ships Tiers 1c/2, a stale high-risk
   constraint fails closed.** The protected procurement demo shows this at its 45-minute
   dwell; tests use a scripted semantic tier.
5. **A redelivered tool call is not executed twice.** Idempotent verdicts (§9.6) stop
   conflicting *decisions*, but on their own a redelivered message would execute its
   tool again under the stored ALLOW: the baseline's double order (ADR 0004). The
   checkpoint response carries `Idempotent-Replayed: true|false`. When a `TOOL_CALL`
   verdict is replayed and allowing, `guard_tool` raises `DuplicateToolCall` instead of
   running the tool; the worker acknowledges the message. This is at-most-once per gated
   call: if a consumer died *after* the verdict but *before* the tool ran, the action is
   not retried. For money-moving tools that is the fail-safe side.
   `reexecute_redelivered_tools=True` opts out.
6. **A duplicate must be the same request.** A request with a stored verdict's key but
   different constraints (full content), envelope digest, hop identity or proposed action
   is not a redelivery. It could be tampering in transit, or a re-executed agent
   proposing a different amount. It is checked afresh and recorded as a non-final
   `WARNING{conflict}`; the stored verdict stays the key's answer, and the conflicting
   request does not update the hop history. Task text and timestamps are left out of the
   fingerprint, so an agent that rewords its task on re-execution still gets the stored
   verdict (§9.6 holds as written).
7. **Loss in transit, case (d)** (§9.5). A `PUBLISH` may name its destination queue
   (`TransportMeta.destination`, a new optional field). `SweepTransitLoss` is a
   background reader of the event log. It flags each such publish with no `CONSUME` of
   that envelope within `CL_CONSUME_TIMEOUT_S` as a `WARNING{transit_loss}` carrying a
   `MISSING_HOP` failure, blamed on the queue edge. Publishes whose verdict blocked or
   re-grounded never left the agent and are not expected downstream. The sweeper rebuilds
   its state from offset 0 on start, so it never flags a hop twice. It runs in the app
   lifespan every `CL_SWEEP_INTERVAL_S` (0 = off).
8. **Redis-worker adapter** (§7.3): `sdk.GuardedWorker` works on any `QueueTransport`. It
   reclaims pending messages *before* reading new ones, so it never processes its own
   in-flight message twice. It runs CONSUME with the delivery's transport metadata, the
   handler inside a hop context (tool gate), then PUBLISH with the destination, with
   bounded re-grounding. It acknowledges on a final decision. It does **not** acknowledge
   a held CONSUME, or a *local* fail-closed verdict (the service could not answer), so a
   transient outage blocks without dropping the task. `CheckpointReply` exposes
   `replayed` and `local`.
9. **Simulated dwell on real Redis Streams.** `RedisStreamsTransport(clock=...)` stamps
   stream ids from a virtual clock (`<ms>-*`), so a 45-minute dwell can be reproduced in
   tests and demos through the real adapter. Production leaves the clock unset and Redis
   assigns ids; the module docstring marks it as simulation only.
10. **Replay folds incrementally**: each checkpoint is re-checked against the state and
    history *as it was when it arrived*, at its original receive time. Holds, inversions
    and staleness replay exactly. `ReplayItem` also compares the held flag.
11. **Test environment.** `hypothesis` is pinned below 6.156: newer releases ship a native
    extension that Windows Application Control blocks on the development host. On that
    host SQLAlchemy is installed without its C extension for the same reason (local venv
    only; the lockfile is unchanged).

## Consequences

- Exit criteria met: `tests/property/test_async_properties.py` (idempotency under any
  redelivery order, including concurrent duplicates; the reordering frontier never moves
  backwards; replay reproduces every decision, holds included) and
  `tests/e2e/test_failure_injection.py` (service unreachable, event store outage, Redis
  outage mid-trace, tampered or stripped envelope in transit, lost message, reordering,
  concurrent duplicate delivery).
- The protected procurement pipeline blocks every baseline violation with no false
  blocks (with a dwell under the threshold), and places one order where the baseline
  places two.
- **Known gaps, kept visible:** judge-timeout and malformed-judge-output injection wait
  for the Tier 2 judge (Phase 5). An event-store *append* failure still returns a
  degraded verdict that the worker acknowledges (ADR 0005 item 8); only read failures,
  surfaced as 503, are retried by redelivery. A real-Redis integration run needs
  `REDIS_URL` (no Docker or Redis on the development host).
