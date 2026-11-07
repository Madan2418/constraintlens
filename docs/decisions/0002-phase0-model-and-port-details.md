# ADR 0002: Model and port details left open by the spec (Phase 0)

- **Status:** accepted
- **Date:** 2026-11-07
- **Spec reference:** ARCHITECTURE.md §6.3, §8, §9

## Context

§8 shows the data models "in abbreviated form" and §6.3 lists ports by responsibility
only. Phase 0 has to fix concrete types and signatures.

## Decision

**Models (`domain/models.py`)**

1. `ProposedAction` = `{tool, args}`, matching the benchmark record format (§13.5).
   `TransportMeta` = `{message_id, delivery_count, enqueued_at, consumed_at}` (§9.2).
2. `EventDraft` is an event without `event_id`. The store assigns ids, so callers cannot forge them.
3. `JudgeResult`, `Delivery` and `Attribution` are models (§7.4, §9.1, §7.7 step 5).
4. Invariants are enforced at the boundary (§5 "illegal states unrepresentable"):
   timezone-aware datetimes; SHA-256 hex for `content_hash` / `envelope_digest`;
   `expiry > issued_at`; a `Limit` has at least one bound and `min ≤ max`;
   `requires_approval_above_limit` needs a `max_value`; no self- or duplicate parents;
   unique constraint ids per envelope; `proposed_action` required at `TOOL_CALL`;
   `envelope.trace_id` equals the request `trace_id`.
5. Verdict postconditions (§5 "design by contract"): scores in [0, 1], non-negative
   latencies, non-empty reason, `RE_GROUND` carries `regrounded_text`, and
   `RE_GROUND` / `BLOCK` / `ESCALATE` need a failure **or** `degraded = true`.
   (Degraded fail-closed blocks legitimately have no failure.)
6. `Verdict.checkpoint` uses the `Checkpoint` literal instead of `str`. That is
   stricter, but it accepts the same valid values.
7. An envelope with an **empty** constraint list is valid, so Tier 0 can report
   `CONSTRAINT_LOST` rather than the request failing validation (§7.2, §9.5).
8. `CheckpointRequest.hop_id` is *not* required to equal `envelope.hop.hop_id`. At
   `CONSUME` the consuming hop differs from the emitting hop recorded in the envelope (§9.7).
9. Clock skew between pipeline and service is not validated at the model layer
   (`consumed_at` may precede `enqueued_at`). The async guard handles it in Phase 4.

**Ports (`domain/ports.py`)**

1. I/O ports (`Judge`, `EventStore`, `QueueTransport`) are `async`. CPU-bound or
   in-memory ports (`Embedder`, `NLIModel`, `PipelineAdapter`, `Clock`) are sync, and the
   application layer offloads heavy model calls to a thread.
2. `NLIModel` is a separate port. §6.3's table lists only `Embedder`, but §6.2,
   §7.4 and §17 (`tier1c_nli.py` "depends on Embedder/NLI port") require an NLI scorer,
   and embeddings must never yield ENTAILED (§7.4 design note).
3. `Judge` exposes `model_id` and `prompt_version` because both are part of the Tier 2
   cache key (§7.4). The structured-output *extraction* call (§7.1) is added to the
   port in Phase 1, when it is first used.
4. `QueueTransport` mirrors Redis Streams: `ensure_group`, `publish`, `consume`
   (new messages), `reclaim` (pending messages idle ≥ `min_idle_ms`, count +1), `ack`.
5. Fakes live in `adapters/` beside the real adapters (§17 names
   `judge/scripted.py`, `store/memory.py`, `transport/memory.py`). `HashEmbedder`
   and `ScriptedNLI` are in `adapters/models/fakes.py`, `StubAdapter` in
   `adapters/pipeline/stub.py`, `SystemClock` / `FrozenClock` in `adapters/clock.py`.

## Consequences

Contract suites in `tests/contract/` pin these signatures. Real adapters added in
later phases are appended to each suite's factory list and must pass unchanged.
