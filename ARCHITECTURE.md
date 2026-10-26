# ConstraintLens — System Architecture


**ConstraintLens: A Runtime Framework for Detecting Semantic Constraint Drift in Asynchronous Multi-Agent AI System**

**Detecting, Localizing, and Benchmarking Semantic Constraint Drift in Asynchronous Multi-Agent AI Systems**

| | |
|---|---|
| **Repository** | `constraintlens` |
| **Benchmark package** | `driftchain-bench` (shipped inside the repo, releasable separately) |
| **Language / runtime** | Python 3.11+ |
| **Document status** | Version 1.0 — architecture baseline |
| **SDG alignment** | SDG 9 (Target 9.5) primary; SDG 16 (Target 16.6) secondary |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Goals, Non-Goals, and Scope](#3-goals-non-goals-and-scope)
4. [Formal Definitions](#4-formal-definitions)
5. [Design Principles](#5-design-principles)
6. [System Architecture](#6-system-architecture)
7. [Component Design](#7-component-design)
8. [Data Models](#8-data-models)
9. [Asynchronous Semantics](#9-asynchronous-semantics)
10. [Failure Policy and Degradation](#10-failure-policy-and-degradation)
11. [Service API](#11-service-api)
12. [Storage and Audit](#12-storage-and-audit)
13. [Benchmark: driftchain-bench](#13-benchmark-driftchain-bench)
14. [Evaluation Design](#14-evaluation-design)
15. [Dashboard](#15-dashboard)
16. [CLI](#16-cli)
17. [Repository Layout](#17-repository-layout)
18. [Configuration](#18-configuration)
19. [Deployment](#19-deployment)
20. [Testing Strategy](#20-testing-strategy)
21. [Security and Privacy](#21-security-and-privacy)
22. [Observability](#22-observability)
23. [Implementation Roadmap](#23-implementation-roadmap)
24. [Risks and Mitigations](#24-risks-and-mitigations)
25. [Related Work and Positioning](#25-related-work-and-positioning)
26. [Glossary](#26-glossary)

---

## 1. Executive Summary

Autonomous AI agents are increasingly delegated real-world authority: approving refunds, moving money, accessing records. Users and organizations attach **constraints** to these tasks ("refund up to $100 without approval"). In realistic deployments a task is rarely handled by one agent. It is delegated across several agents, often through message queues with time gaps, and each agent re-expresses the task in its own words before acting or delegating further.

Existing safeguards check that a constraint's *structure* is still present (a field or token scope was not widened) or that a request is acceptable *once*, at the start. Neither verifies that the constraint's **meaning survives the journey**. A task can be paraphrased, summarized, or rewritten across hops so that the final tool call exceeds the user's intent even though no field was deleted and no scope was technically widened. Nothing errors, nothing crashes, and nothing is logged.

**ConstraintLens** is a runtime, framework-agnostic verification layer that:

1. **Detects** semantic constraint drift continuously at every delegation boundary and at the pre-tool-call gate.
2. **Localizes** drift by identifying the hop and agent responsible (drift attribution).
3. **Composes** multiple concurrent constraints (user, organization, system) and detects when one is silently dropped.
4. **Benchmarks** the problem by releasing `driftchain-bench`, a labeled dataset of asynchronous delegation chains with known drift.
5. **Enforces** a decision (allow, re-ground, or block) before a drifted action executes, and records every decision in an append-only audit log.

The system is delivered as a Python service (FastAPI) with a thin SDK, adapters for LangGraph and plain Redis workers, a React dashboard for live demonstration, a CLI, and a reproducible evaluation harness.

---

## 2. Problem Statement

Agentic systems attach explicit constraints (spending limits, approval requirements, scope restrictions) to delegated tasks. As the task passes through multiple agents, often asynchronously through message queues, callbacks, and time-delayed handoffs, each agent re-interprets and re-expresses it. Existing mechanisms verify that a constraint's structural representation is not explicitly widened between agents, or perform a one-shot semantic match at task start. No existing mechanism *continuously* verifies that the constraint's meaning is preserved across asynchronous hops.

The resulting failure class is silent: an agent's tool call drifts outside the user's original intent while every structural check still passes. It is most dangerous after asynchronous delays, where context can go stale, messages can be reordered, and the agent consuming a task no longer sees the context in which the constraint was issued.

### 2.1 Worked Example

```
User:    "Refund this customer, up to $100. Anything above needs manager approval."

Hop 1  Intake Agent        T1: "Process refund for order #4471, limit $100, approval above."
        -- Redis Stream, 12 minute delay --
Hop 2  Triage Agent        T2: "Customer is upset about order #4471. Resolve with a refund."
        -- Redis Stream --
Hop 3  Payments Agent      T3: "Refund order #4471 in full."
                           Tool call: issue_refund(order=4471, amount=2000)

Structural check:      constraint object present .............. PASS
Token scope check:     scope not widened ...................... PASS
One-shot gate (hop 1): task acceptable at start ............... PASS
ConstraintLens:        T2 no longer entails "max $100" ........ DRIFT at Hop 2
                       A3 amount=2000 > limit 100 ............. ACTION VIOLATION at Hop 3
                       Decision: BLOCK; culprit: Hop 2 (Triage Agent)
```

The silent failure is invisible to structural checks. ConstraintLens catches it at two places and attributes it to the hop that introduced it.

---

## 3. Goals, Non-Goals, and Scope

### 3.1 Goals

| ID | Goal |
|---|---|
| G1 | Detect semantic constraint drift at every delegation boundary, including asynchronous queue boundaries. |
| G2 | Detect action violations at the pre-tool-call gate, independent of text drift. |
| G3 | Attribute detected drift to the specific hop and agent that introduced it. |
| G4 | Track multiple constraints per task and detect silent loss of any one of them. |
| G5 | Remain framework-agnostic: integrate with a pipeline by adding a few lines of SDK code. |
| G6 | Enforce decisions before execution (block / re-ground / allow), with a configurable failure policy. |
| G7 | Produce a complete, replayable audit trail. |
| G8 | Release a reproducible benchmark and an evaluation harness with quantitative results. |
| G9 | Keep added latency within an explicit per-tier budget, measured and reported. |

### 3.2 Non-Goals

- Cryptographic signing or authentication of delegation chains. That is a separate problem addressed by delegation-credential protocols.
- Detecting hallucination or general output quality problems unrelated to constraints.
- Replacing agent frameworks, or modifying agent prompts as a primary mechanism.
- Packaging as a polished installer or hosted product. A reproducible local and Docker deployment is the target.
- Learning-based policy discovery. Constraints are extracted or declared, not mined.

### 3.3 Scope Boundaries

| In scope | Out of scope |
|---|---|
| Numeric limits, approval requirements, resource/scope restrictions, expiry | Free-form behavioral norms ("be polite") |
| Text-mediated drift across hops | Pure model-weight or training-time failures |
| Redis Streams async transport; direct (in-process) handoff | Kafka, SQS, and other transports (port exists; adapters not built) |
| LangGraph and plain Redis workers | CrewAI, AutoGen (port is designed to admit them) |

---

## 4. Formal Definitions

Precise definitions are required so evaluation metrics are unambiguous.

### 4.1 Core Entities

| Symbol | Name | Definition |
|---|---|---|
| **C** | Constraint | A structured object: `action`, `resource`, `limit`, `authority`, `scope`, `issued_at`, `expiry`, plus identity and ownership fields. |
| **𝒞** | Constraint set | The set of constraints attached to a trace, each with a distinct `constraint_id`. |
| **H_k** | Hop *k* | **One agent step**: the interval in which a single agent receives a task, reasons or acts on it, and emits a delegation or a tool call. Queue publish and consume are *checkpoints inside* a hop boundary, not separate hops. |
| **T_k** | Task text | The natural-language task description as emitted by hop *k*. T_0 is the user's original instruction. |
| **A_k** | Proposed action | The structured tool call hop *k* is about to execute (name plus arguments). |
| **Trace** | Delegation trace | The directed acyclic graph of hops sharing a `trace_id`. Fan-out and fan-in are permitted. |
| **E_k** | Entailment score | A value in [0, 1] estimating the degree to which T_k still entails C. |

### 4.2 Two Distinct Failures

| Failure | Definition | Role |
|---|---|---|
| **Textual drift** | For some constraint C in 𝒞, T_k fails to entail C (E_k below threshold, or judged CONTRADICTED / not entailed), though T_{k-1} entailed it. | Early warning. Identifies where meaning was lost. |
| **Action violation** | A_k breaks C, whether or not any text drifted. | The actual harm. Enforcement is decisive here. |
| **Constraint loss** | A constraint in 𝒞 is absent from the envelope or its hash no longer matches the issued value. | Structural failure, caught by Tier 0. |
| **Staleness** | The constraint's age at consumption exceeds its freshness bound without re-verification. | Async-specific failure. |

Keeping textual drift and action violation separate gives a better attribution story: the culprit hop for *drift* is where meaning was lost, while the culprit for a *violation* is where the bad action was proposed. They are often different hops.

### 4.3 Verdict

A **verdict** is the tuple `(trace_id, hop_id, checkpoint, tier_used, failure_types, scores, decision, degraded, reason)`. Verdicts are idempotent, keyed by `(trace_id, hop_id, checkpoint)`.

### 4.4 Ground Truth for Evaluation

For benchmark chains, labels are known by construction (Section 13): each chain carries the hop at which a mutation was applied and the mutation operator used. Detection is counted correct when the system flags the chain at or after the labeled hop and before the harmful action; attribution is counted correct when the predicted culprit hop equals the labeled hop.

---

## 5. Design Principles

Each principle is tied to a concrete structural decision.

| Principle | Where it shows up |
|---|---|
| **Hexagonal architecture (ports and adapters)** | The domain core imports no FastAPI, Redis, SQLite, LangGraph, or LLM SDK. It depends only on ports. LangGraph and plain-Redis integrations are two adapters on the same `PipelineAdapter` port. |
| **Dependency inversion** | Adapters depend on core interfaces, never the reverse. Wiring happens in a single composition root. |
| **Single responsibility** | Extractor, checker, attributor, composer, enforcer, and recorder are separate modules with one reason to change each. |
| **Open/closed** | New checker tiers, mutation operators, and adapters are added by registration, not by editing existing code. |
| **Interface segregation** | Small ports (`Judge`, `EventStore`, `QueueTransport`, `Clock`) so every adapter is a true substitute. |
| **Fail-safe defaults** | When uncertain on a high-risk action, deny. Degraded verdicts are explicitly marked. |
| **Make illegal states unrepresentable** | Pydantic v2 models reject malformed constraints and envelopes at the boundary. The core never handles invalid data. |
| **Design by contract** | Checkers have explicit postconditions (e.g., every verdict carries tier, score, and reason). Violations raise immediately in tests. |
| **Idempotency** | Verdicts keyed by `(trace_id, hop_id, checkpoint)` so at-least-once delivery cannot produce conflicting decisions. |
| **Single source of truth** | The append-only event log is the only record of what happened. Dashboard, attribution, metrics, and replay are derived views. |
| **Separation of detection, decision, recording** | Checker produces a verdict, enforcer maps it to an action, recorder persists both. They never mix. |
| **KISS / YAGNI** | A port is introduced only where a second implementation is realistic: transport, judge, store, framework adapter, clock. Everything else is a plain module. |
| **Twelve-factor configuration** | All thresholds, keys, URLs, and budgets come from environment-backed config, so evaluation runs are reproducible. |

---

## 6. System Architecture

### 6.1 Context

```
                          +-----------------------------+
   User instruction ----> |   Customer pipeline         |
                          |  (LangGraph / Redis workers)|
                          |                             |
                          |   Agent A --> Agent B --> Agent C --> tools
                          |      |  ^        |  ^         |
                          +------|--|--------|--|---------+
                                 |  |        |  |
                         SDK calls (HTTP, async)  
                                 v  |        v  |
                    +---------------------------------------+
                    |        ConstraintLens Service         |
                    |   (FastAPI, hexagonal domain core)    |
                    +---------------------------------------+
                       |            |             |
                  Redis Streams   SQLite      LLM Judge API
                  (transport)   (event log)   (Tier 2, cached)
                                    |
                              WebSocket / REST
                                    |
                            React Dashboard, CLI
```

The pipeline continues to own its agents and queues. ConstraintLens is called at defined checkpoints through the SDK and returns a decision. It does not sit in the data path of the queue itself; it observes and gates through explicit calls.

### 6.2 Layered View

```
+--------------------------------------------------------------+
|  Delivery layer        FastAPI routes - WebSocket - CLI      |
+--------------------------------------------------------------+
|  Application layer     Use cases: VerifyCheckpoint,          |
|                        OpenTrace, AttributeTrace, RunEval    |
+--------------------------------------------------------------+
|  Domain core (pure)    Constraint - Envelope - Verdict       |
|                        TieredChecker - ActionGate            |
|                        AsyncGuard - Composer - Attributor    |
|                        Enforcer - Policy                     |
+--------------------------------------------------------------+
|  Ports                 Judge - EventStore - QueueTransport   |
|                        PipelineAdapter - Clock - Embedder    |
+--------------------------------------------------------------+
|  Adapters              ClaudeJudge/OpenAIJudge - SQLiteStore |
|                        RedisStreamsTransport                 |
|                        LangGraphAdapter - RedisWorkerAdapter |
|                        SentenceTransformerEmbedder - NLI     |
+--------------------------------------------------------------+
```

Dependencies point downward through ports only: delivery calls application, application calls domain, domain declares ports, and adapters implement them.

### 6.3 Ports

| Port | Responsibility | Initial adapters | Fake for tests |
|---|---|---|---|
| `Judge` | Return ENTAILED / CONTRADICTED / AMBIGUOUS with reason for (text, constraint) | Anthropic API, OpenAI-compatible API | `ScriptedJudge` |
| `EventStore` | Append events; read by trace; stream by offset | SQLite (SQLAlchemy) | `InMemoryStore` |
| `QueueTransport` | Publish/consume envelopes with delivery metadata | Redis Streams | `InMemoryQueue` |
| `PipelineAdapter` | Read/write the envelope within a framework's native message or state | LangGraph, plain Redis worker | `StubAdapter` |
| `Embedder` | Encode text to vectors (routing only) | sentence-transformers | `HashEmbedder` |
| `Clock` | Current time, monotonic time | System clock | `FrozenClock` |

### 6.4 Checkpoints

Three checkpoint types exist. Each is a point where the SDK calls the service.

| Checkpoint | Position | Purpose | Enforcement strength |
|---|---|---|---|
| `PUBLISH` (egress) | Just before an agent publishes a task to the next agent or queue | Verify T_k still entails 𝒞 before it leaves the agent; supports attribution | Advisory by default; blocking configurable |
| `CONSUME` (ingress) | Just after an agent receives a task from a queue or callback | Detect staleness, reordering, loss in transit; re-verify after delay | Re-ground or block on stale or lost constraint |
| `TOOL_CALL` (gate) | Immediately before a tool executes | Check A_k against 𝒞 directly and re-check T_k | **Decisive**: the only checkpoint where enforcement is mandatory |

This three-point structure is deliberate: publish and consume expose *where* drift entered, while the tool-call gate guarantees that no drifted action executes even if earlier checkpoints were skipped or degraded.

---

## 7. Component Design

### 7.1 Constraint Extraction

**Input:** the user's instruction (natural language) plus optional organization and system policy declarations.
**Output:** one or more validated `Constraint` objects.
**Runs:** once, at trace creation (`OpenTrace` use case).

Pipeline:

1. **Rule-based extractor** (fast path): regular expressions and parsers for currency limits, approval thresholds, time windows, and resource scopes. Handles common patterns deterministically.
2. **LLM extractor** (fallback and enrichment): a few-shot prompted call through the `Judge` port's structured-output interface that fills the schema. Output is validated by Pydantic; invalid output triggers one repair retry, then rejection.
3. **Reconciliation:** if both paths produce values, disagreement on a numeric limit is resolved to the *more restrictive* value and flagged for review.
4. **Canonicalization:** each constraint gets a `canonical_text` (a normalized plain-language restatement) and a `content_hash` (SHA-256 over the canonical JSON). Both ride in the envelope.

Declared constraints (e.g., an organization policy of "refunds never exceed $50") bypass extraction and enter through configuration with `scope = org` or `system`.

### 7.2 Constraint Carrier and Envelope

The constraint set travels in the **envelope**, a metadata structure kept strictly separate from the natural-language payload. Agents never need to parse it; adapters read and write it.

```
Envelope
 |- trace_id, hop_id, parent_hop_ids[], hop_seq
 |- agent_id, issued_at, emitted_at
 |- constraints[]  (Constraint objects, each with content_hash)
 |- envelope_digest  (hash over constraints + trace identity)
 |- payload  (natural-language task text T_k, structured args if any)
```

Rules:

- The envelope is created at the root and **never rewritten by agents**. Adapters append hop metadata; constraint objects are read-only after issue.
- Tier 0 recomputes each constraint's hash and compares against the value registered at trace open, so tampering and truncation are both detectable.
- The service stores the authoritative copy of 𝒞 per trace. The envelope copy is what must *match* it. This is what makes "constraint loss" detectable even when the envelope is stripped.

### 7.3 SDK and Integration Adapters

A thin Python SDK (`constraintlens.sdk`) is the only thing a pipeline author touches. It offers three forms of integration.

**Decorator / context manager (framework-neutral):**

```python
from constraintlens.sdk import ConstraintLens

cl = ConstraintLens(base_url="http://localhost:8000")

@cl.guard_hop(agent_id="triage")
async def triage_agent(task: Task) -> Task:
    ...  # agent logic unchanged

@cl.guard_tool(name="issue_refund")
async def issue_refund(order: int, amount: float): ...
```

**LangGraph adapter:** wraps graph nodes and the tool-execution node, reading the envelope from graph state and publishing `CONSUME` / `PUBLISH` / `TOOL_CALL` checkpoints around each node.

**Redis worker adapter:** a consumer-side wrapper around a Redis Streams consumer loop that calls `CONSUME` before handing the message to the worker, and `PUBLISH` before the worker emits downstream.

Both adapters implement `PipelineAdapter`. The SDK uses an async HTTP client with timeouts and a local circuit breaker; failure behavior is governed by the failure policy (Section 10).

### 7.4 Tiered Drift Checker

The checker is a chain of tiers executed in order. Each tier either produces a **terminal verdict** or **escalates**. Tiers are registered, not hard-coded (open/closed).

> **Design note.** Embedding similarity is a weak entailment test: "refund up to $100" and "refund up to $1000" are nearly identical in embedding space, and negation barely moves similarity. Embeddings are therefore used only for **routing** (a fast signal of "obviously unrelated" or "obviously equivalent"), never as the sole basis for allowing a task through.

| Tier | Name | Method | Cost | Terminal outcomes |
|---|---|---|---|---|
| **0** | Deterministic integrity | Envelope present, constraint hash matches registered value, not expired, `hop_seq` valid, trace open | microseconds | `LOST`, `EXPIRED`, `OUT_OF_ORDER`, or pass-through to Tier 1 |
| **1a** | Structured comparison | When A_k has structured arguments, compare directly against `limit`, `resource`, `action` (exact, free) | microseconds | `VIOLATION` or pass |
| **1b** | Lexical constraint extraction | Extract numbers, units, qualifiers, and negations from T_k; compare to C's fields (e.g., limit missing, limit changed, "approval" dropped) | sub-millisecond | `DRIFT` (limit changed or qualifier lost), or pass |
| **1c** | NLI cross-encoder | Local natural-language-inference model scores whether T_k entails `canonical_text(C)` | tens of ms | `ENTAILED` (high), `NOT_ENTAILED` (low), or escalate (band) |
| **2** | LLM judge | Structured-prompt judge returns ENTAILED / CONTRADICTED / AMBIGUOUS with a one-line reason; verdicts cached | hundreds of ms to seconds | Terminal verdict |

Routing and escalation rules:

1. Tier 0 failures are terminal and need no semantic work.
2. If A_k has structured arguments, Tier 1a runs first and its result is authoritative for the action-violation failure.
3. Tier 1b and 1c assess **textual drift**. If 1c's entailment score lies in the **uncertainty band** `[θ_low, θ_high]`, escalate to Tier 2. Above `θ_high` the task is ENTAILED; below `θ_low` it is NOT_ENTAILED.
4. Embedding similarity is used to short-circuit only the *clearly irrelevant* case (text that no longer mentions the constrained action at all) and to select cache keys; it never produces an ENTAILED verdict on its own.
5. If Tier 2 returns AMBIGUOUS, the policy for the constraint's risk class decides (high risk: treat as drift; low risk: allow with warning).

**Judge contract.** The judge prompt is versioned (`judge_prompt_v1`). Input: `canonical_text(C)`, `T_k`, and optionally `A_k`. Output: strict JSON `{verdict, reason, confidence}`. Output that fails schema validation is retried once, then treated as a judge failure (Section 10).

**Cache.** Key = `hash(canonical_text(C) || normalized(T_k) || prompt_version || judge_model)`. Backed by the `EventStore` or a dedicated table. Cache hit rate is a reported metric. Cache is disabled in specific evaluation runs where independence matters.

**Thresholds.** `θ_low` and `θ_high` are not guessed. They are tuned on a held-out validation split of `driftchain-bench` (Section 14.4), selecting the band that maximizes recall subject to a false-block ceiling, and fixed before the test split is touched.

### 7.5 Async Guard

Handles failure modes specific to asynchronous transport. Detailed semantics are in Section 9. Summary of responsibilities:

- **Staleness:** compare constraint age at `CONSUME` against freshness bound; require re-verification (Tier 1c or 2) when exceeded.
- **Reordering:** detect gaps and inversions in `hop_seq`; hold or flag.
- **Loss in transit:** detect missing envelopes or constraints, and missing hops (gap in `hop_seq` or an unacknowledged publish).
- **Redelivery:** recognize duplicate deliveries via `(trace_id, hop_id)` and return the stored verdict.

### 7.6 Constraint Composition

Real tasks carry several constraints at once, for example a user limit of $100 and an organization policy of $50.

**Tracking.** Each constraint keeps its own `constraint_id`, `owner`, and `scope` (`user`, `org`, `system`). All are checked individually at every checkpoint, so loss of one constraint is detected even if another survives.

**Resolution (effective constraint).** Per dimension, the most restrictive value wins:

| Dimension | Rule |
|---|---|
| Numeric limit | minimum |
| Allowed action set | intersection |
| Resource scope | intersection |
| Authority / approval | strictest (approval required if *any* constraint requires it) |
| Expiry | earliest |

**Scope precedence.** `system` > `org` > `user` for *conflict reporting* only: a user constraint that is looser than an org constraint is recorded as `SUPERSEDED` and does not weaken enforcement. Most-restrictive-wins applies regardless of scope.

**Dropped-constraint detection.** At each checkpoint the set of constraint IDs present (in the envelope and as entailed by T_k) is compared with the registered set. Missing IDs produce a `CONSTRAINT_DROPPED` failure naming the specific constraint.

**Conflict detection.** Constraints that cannot be simultaneously satisfied (e.g., required approval vs. "no human in the loop") are flagged at trace open as `UNSATISFIABLE` and the trace is refused.

### 7.7 Drift Attribution

Goal: when a trace fails, name the hop and agent that introduced the failure.

**Model.** A trace is a DAG of hops. Each hop *k* has an entailment score vector with one score per constraint, `E_k[c]`, produced by the checker at `PUBLISH` (and at `CONSUME` for the downstream hop).

**Algorithm.**

1. For each constraint *c*, compute the edge delta `Δ(k→j) = E_k[c] − E_j[c]` for every edge (parent *k*, child *j*).
2. The **culprit hop** for *c* is the first hop in topological order whose entailment crosses below threshold *and* whose incoming delta exceeds `δ_min` (to avoid blaming noise).
3. **Fan-in:** a hop with multiple parents takes the minimum parent score as its baseline; if the child's score is below *all* parents' scores, the child is the culprit; if it matches the worst parent, blame propagates to that parent.
4. **Async attribution:** if drift first appears at a `CONSUME` checkpoint with no preceding text change, the cause is classified `STALE_CONTEXT` or `TRANSIT_LOSS` and blamed on the queue edge rather than an agent.
5. **Output:** `Attribution { constraint_id, culprit_hop, culprit_agent, cause ∈ {REWRITE, STALE_CONTEXT, TRANSIT_LOSS, INJECTION, UNKNOWN}, delta, evidence_events[] }`.

Attribution runs as a view over the event log (`AttributeTrace` use case), so it can be recomputed offline with new parameters during evaluation. It is computed lazily on failure for live traces and in bulk for benchmark runs.

### 7.8 Enforcement

The enforcer maps a verdict and the constraint's risk class to a decision.

| Decision | Behavior |
|---|---|
| `ALLOW` | Pass through untouched. |
| `ALLOW_WITH_WARNING` | Pass through; emit a warning event (used for low-risk and degraded cases). |
| `RE_GROUND` | Return the canonical constraint text to the caller for re-injection into the agent's context; the agent retries; the retry is checked again. Bounded by `max_regrounds`. |
| `BLOCK` | Reject the checkpoint. At `TOOL_CALL`, the SDK raises `ConstraintViolation`, so the tool does not execute. |
| `ESCALATE` | Block and route to a human-approval hook (webhook/callback) when authority requires approval. |

Decision table (defaults, overridable per constraint):

| Failure | High-risk constraint | Low-risk constraint |
|---|---|---|
| Action violation (A_k breaks C) | BLOCK | BLOCK |
| Textual drift (NOT_ENTAILED / CONTRADICTED) at TOOL_CALL | BLOCK | RE_GROUND |
| Textual drift at PUBLISH / CONSUME | RE_GROUND | ALLOW_WITH_WARNING |
| Constraint loss / dropped / hash mismatch | BLOCK | BLOCK |
| Stale beyond bound, re-verification passes | ALLOW | ALLOW |
| Stale beyond bound, re-verification fails | BLOCK | RE_GROUND |
| AMBIGUOUS after Tier 2 | BLOCK | ALLOW_WITH_WARNING |
| Judge unavailable (degraded) | per fail policy | per fail policy |

### 7.9 Recorder

Every verdict, enforcement decision, and checkpoint event is appended to the event log (Section 12) *after* the decision is made and *before* the response is returned, so a decision never exists without a record. A recorder failure on a high-risk constraint fails closed.

---

## 8. Data Models

All models are Pydantic v2 with `extra="forbid"` and frozen where immutable. Shown here in abbreviated form; the source of truth is `constraintlens/domain/models.py`.

### 8.1 Constraint

```python
class Limit(BaseModel):
    max_value: Decimal | None
    min_value: Decimal | None = None
    unit: str                       # "USD", "INR", "records", ...

class Constraint(BaseModel, frozen=True):
    constraint_id: UUID
    scope: Literal["user", "org", "system"]
    owner: str                      # principal that issued it
    subject: str                    # who/what it applies to
    action: str                     # "refund" | "transfer" | "delete" | ...
    resource: str                   # "order" | "customer_account" | ...
    limit: Limit | None
    authority: Literal["requires_approval_above_limit",
                       "requires_approval_always", "none"]
    risk_class: Literal["high", "low"] = "high"
    origin: Literal["user", "upstream_agent", "policy"]
    canonical_text: str
    content_hash: str               # SHA-256 over canonical JSON
    issued_at: datetime
    expiry: datetime | None
    max_age_seconds: int | None     # freshness bound for async hops
```

### 8.2 Envelope and Hop

```python
class HopMeta(BaseModel, frozen=True):
    hop_id: str
    parent_hop_ids: list[str]
    hop_seq: int                    # monotonic per trace path
    agent_id: str
    emitted_at: datetime

class Envelope(BaseModel):
    trace_id: UUID
    hop: HopMeta
    constraints: list[Constraint]
    envelope_digest: str
    payload_text: str               # T_k
    payload_args: dict | None       # structured args if available
```

### 8.3 Checkpoint Request and Verdict

```python
class CheckpointRequest(BaseModel):
    trace_id: UUID
    hop_id: str
    checkpoint: Literal["PUBLISH", "CONSUME", "TOOL_CALL"]
    envelope: Envelope
    proposed_action: ProposedAction | None   # required at TOOL_CALL
    transport_meta: TransportMeta | None     # delivery count, enqueue time

class Failure(BaseModel):
    kind: Literal["ACTION_VIOLATION", "TEXTUAL_DRIFT", "CONSTRAINT_LOST",
                  "CONSTRAINT_DROPPED", "HASH_MISMATCH", "EXPIRED",
                  "STALE", "OUT_OF_ORDER", "MISSING_HOP", "UNSATISFIABLE"]
    constraint_id: UUID | None
    detail: str

class Verdict(BaseModel, frozen=True):
    verdict_id: UUID
    trace_id: UUID
    hop_id: str
    checkpoint: str
    tier_used: Literal["T0", "T1a", "T1b", "T1c", "T2"]
    scores: dict[UUID, float]                # per-constraint entailment
    failures: list[Failure]
    decision: Literal["ALLOW", "ALLOW_WITH_WARNING", "RE_GROUND",
                      "BLOCK", "ESCALATE"]
    degraded: bool                           # a tier was skipped or failed
    reason: str
    latency_ms: dict[str, float]             # per tier
    cache_hit: bool
    regrounded_text: str | None
```

### 8.4 Event

```python
class Event(BaseModel, frozen=True):
    event_id: int                   # monotonic sequence (store-assigned)
    trace_id: UUID
    type: Literal["TRACE_OPENED", "CONSTRAINT_REGISTERED", "CHECKPOINT_RECEIVED",
                  "TIER_EXECUTED", "VERDICT_ISSUED", "DECISION_ENFORCED",
                  "REGROUND_ISSUED", "ATTRIBUTION_COMPUTED", "TRACE_CLOSED",
                  "JUDGE_FAILED", "WARNING"]
    hop_id: str | None
    occurred_at: datetime
    payload: dict                   # type-specific, schema-validated
```

---

## 9. Asynchronous Semantics

The queue boundary is where this system's novelty concentrates, so its behavior is specified exactly.

### 9.1 Transport

Redis Streams with consumer groups, accessed through the `QueueTransport` port. Redis Streams provide **at-least-once delivery**: a message can be delivered more than once (consumer crash before ack, pending-entry reclaim) and the system must be correct under redelivery.

### 9.2 Identity and Ordering Fields

| Field | Purpose |
|---|---|
| `trace_id` | Groups all hops of one delegated task |
| `hop_id` | Unique per agent step |
| `parent_hop_ids` | Edges of the trace DAG (supports fan-out / fan-in) |
| `hop_seq` | Monotonic sequence along a path, used for gap and inversion detection |
| `issued_at` | When the constraint was issued (root) |
| `emitted_at` | When this hop emitted the message |
| `enqueued_at` / `consumed_at` | Transport timestamps, giving queue dwell time |

### 9.3 Staleness

At `CONSUME`, `age = now − issued_at` and `dwell = consumed_at − enqueued_at`. The constraint is **stale** if `age > max_age_seconds` or `dwell > dwell_threshold`. Stale does not mean violated; it means context may have changed. The response is **mandatory re-verification** of T_k against 𝒞 through Tier 1c / Tier 2 even if earlier checkpoints passed. Expired constraints (`now > expiry`) are a hard `EXPIRED` failure.

### 9.4 Reordering and Gaps

Per trace path, the guard tracks the highest `hop_seq` seen. At `CONSUME`:

- `hop_seq == expected`: normal.
- `hop_seq < expected` (inversion): `OUT_OF_ORDER`. The message may be a late delivery of an earlier hop; it is checked but not allowed to overwrite later state.
- `hop_seq > expected` (gap): `MISSING_HOP`. A predecessor was lost or is delayed. High-risk constraints block until the gap is resolved or a timeout elapses.

For fan-in, a hop is *ready* only when all `parent_hop_ids` have been observed or a configured join timeout expires, in which case the join proceeds with a `MISSING_HOP` warning.

### 9.5 Loss in Transit

Detected by (a) envelope absent, (b) constraint IDs in the registry but not in the envelope, (c) hash mismatch, or (d) a `PUBLISH` event with no corresponding `CONSUME` within `consume_timeout`. Case (d) is detected by a background sweeper over the event log.

### 9.6 Redelivery and Idempotency

Verdicts are keyed by `(trace_id, hop_id, checkpoint)`. On a duplicate request:

1. The service looks up the stored verdict.
2. If present, it returns it unchanged (and logs a `WARNING` with `redelivery=true`).
3. If the earlier verdict is still in flight, the duplicate waits on it (per-key lock) rather than computing a second, possibly conflicting one.

This guarantees that a message delivered twice cannot be allowed once and blocked once.

### 9.7 Callback Returns

A callback return (an agent's response arriving later through a different path from the request) is treated as a `CONSUME` checkpoint on a hop whose `parent_hop_ids` identify the originating request. The causal link is carried explicitly in the envelope rather than inferred from structure, which is what keeps tracing correct when the response path differs from the request path.

---

## 10. Failure Policy and Degradation

### 10.1 Failure Modes

| Component failure | Behavior |
|---|---|
| LLM judge unavailable, timing out, or returning invalid output | Tiers 0, 1a, 1b, 1c still run. Verdict marked `degraded = true`. Result per policy below. |
| NLI model unavailable | Escalate directly to Tier 2 if available; otherwise degraded. |
| Event store unavailable | Fail closed for high-risk constraints; fail open with warning for low-risk. |
| ConstraintLens service unreachable from SDK | Governed by SDK `on_unavailable` setting per constraint risk class. |
| Redis unavailable | Transport-level failure of the pipeline itself; ConstraintLens reports and does not mask it. |

### 10.2 Policy

```
fail_policy:
  high_risk:  closed   # block when verification cannot complete
  low_risk:   open     # allow with warning when verification cannot complete
```

Configurable per constraint via `risk_class` and globally via config. The SDK enforces the policy locally using a circuit breaker so a dead service cannot silently disable protection on high-risk actions.

### 10.3 Latency Budgets

| Tier | Target (p95) | Hard timeout |
|---|---|---|
| Tier 0 / 1a / 1b | < 5 ms | 50 ms |
| Tier 1c (NLI) | < 100 ms | 500 ms |
| Tier 2 (LLM judge) | < 3 s | 10 s |
| End-to-end, no escalation | < 150 ms | n/a |

Budgets are asserted in the evaluation harness and reported alongside accuracy. A timeout is treated as a tier failure and handled by the degradation rules above.

---

## 11. Service API

FastAPI, versioned under `/v1`. All request and response bodies are the Pydantic models in Section 8. OpenAPI documentation is generated and served at `/docs`.

### 11.1 REST Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/traces` | Open a trace: extract or register constraints, return `trace_id` and the initial envelope |
| `POST` | `/v1/traces/{trace_id}/checkpoints` | Submit a checkpoint (`PUBLISH`, `CONSUME`, `TOOL_CALL`); returns a `Verdict` |
| `POST` | `/v1/traces/{trace_id}/close` | Close a trace and trigger final attribution |
| `GET` | `/v1/traces/{trace_id}` | Trace summary: hops, verdicts, decisions |
| `GET` | `/v1/traces/{trace_id}/events` | Ordered event log for the trace |
| `GET` | `/v1/traces/{trace_id}/attribution` | Attribution result(s) |
| `POST` | `/v1/traces/{trace_id}/replay` | Recompute verdicts from stored events with given parameters (no side effects) |
| `GET` | `/v1/metrics` | Aggregates: decisions by type, tier usage, cache hit rate, latency percentiles |
| `POST` | `/v1/eval/runs` | Start an evaluation run (benchmark split, configuration, seed) |
| `GET` | `/v1/eval/runs/{run_id}` | Run status and results |
| `GET` | `/v1/health` | Liveness and dependency status (store, Redis, judge, models) |

Idempotency: `POST .../checkpoints` is idempotent on `(trace_id, hop_id, checkpoint)`; the client may also pass an `Idempotency-Key` header, which takes precedence.

### 11.2 Checkpoint Response Contract

```json
{
  "verdict_id": "b1c4…",
  "decision": "BLOCK",
  "tier_used": "T1a",
  "failures": [
    {"kind": "ACTION_VIOLATION",
     "constraint_id": "7d2e…",
     "detail": "amount=2000 exceeds limit max_value=100 USD"}
  ],
  "scores": {"7d2e…": 0.12},
  "degraded": false,
  "reason": "Proposed action violates constraint 7d2e…",
  "regrounded_text": null,
  "latency_ms": {"T0": 0.4, "T1a": 0.2}
}
```

### 11.3 WebSocket

`GET /v1/stream?trace_id=…` (or without `trace_id` for all traces) opens a WebSocket that pushes events as they are appended to the log. Messages are the `Event` model, delivered in `event_id` order. Clients may resume with `?after_event_id=N`, which is how the dashboard recovers after a reconnect. The stream is read-only.

### 11.4 Errors

RFC 7807 problem-details bodies. Domain errors: `ConstraintViolation` (422), `InvalidEnvelope` (400), `UnknownTrace` (404), `UnsatisfiableConstraints` (409), `DependencyUnavailable` (503, with degraded verdict where applicable).

---

## 12. Storage and Audit

### 12.1 Event Sourcing

The **append-only event log** is the single source of truth. Verdicts, decisions, attributions, metrics, and dashboard state are projections computed from events. Events are never updated or deleted; corrections are new events.

Benefits relevant to this project: any run can be replayed deterministically under different parameters (threshold tuning, ablations), and the audit trail cannot diverge from system behavior because it *is* the system's record.

### 12.2 Schema (SQLite via SQLAlchemy)

```
events
  event_id       INTEGER PRIMARY KEY AUTOINCREMENT
  trace_id       TEXT NOT NULL
  hop_id         TEXT
  type           TEXT NOT NULL
  occurred_at    TEXT NOT NULL            -- ISO-8601 UTC
  payload        TEXT NOT NULL            -- JSON
  INDEX (trace_id, event_id)
  INDEX (type, occurred_at)

traces                                     -- projection
  trace_id       TEXT PRIMARY KEY
  opened_at, closed_at, status
  constraint_ids TEXT                      -- JSON array
  outcome        TEXT                      -- ALLOWED | BLOCKED | DEGRADED

constraints                                -- projection (registered set per trace)
  constraint_id  TEXT PRIMARY KEY
  trace_id       TEXT NOT NULL
  scope, owner, content_hash, body JSON

verdicts                                   -- projection
  verdict_id     TEXT PRIMARY KEY
  trace_id, hop_id, checkpoint
  decision, tier_used, degraded
  UNIQUE (trace_id, hop_id, checkpoint)    -- enforces idempotency

judge_cache
  cache_key      TEXT PRIMARY KEY
  verdict        TEXT, reason TEXT, model TEXT, prompt_version TEXT
  created_at     TEXT

benchmark_chains                           -- loaded benchmark data
  chain_id, split, scenario, mutation_op, mutated_hop, label, body JSON

eval_runs / eval_results                   -- evaluation outputs
```

Projections are rebuilt from `events` by a `project_all()` routine, which is also used in tests to prove that projections are pure functions of the log.

### 12.3 Operational Settings

SQLite runs in WAL mode with a single writer queue to serialize appends. This is sufficient for the target load (single-node evaluation and demo). The `EventStore` port isolates this choice; a PostgreSQL adapter is the documented upgrade path and is deliberately not built.

### 12.4 Retention and Redaction

Payload text may contain personal data. The recorder applies configurable redaction (regex-based masks for emails, card numbers, phone numbers) before persisting `payload_text`. A `store_full_text` flag allows raw storage in development and requires explicit opt-in.

---

## 13. Benchmark: driftchain-bench

A released, reproducible dataset of asynchronous delegation chains with known drift. It serves two purposes: it provides the labeled data for threshold tuning and evaluation, and it is a citable artifact independent of the tool.

### 13.1 Construction Method

Chains are generated by crossing **scenario templates** with **drift mutation operators**. Because mutations are applied programmatically, labels are known by construction, and an LLM is used only for natural paraphrase of the surface text.

```
scenario template  x  mutation operator  x  mutation hop  x  topology  x  async profile
        |                    |                   |             |              |
   base chain           what is lost         where it is    linear /      delay, reorder,
   (constraint          and how              introduced     fan-out /     redelivery
   preserved)                                                fan-in
```

### 13.2 Scenario Templates

| Scenario | Constraint types | Typical harmful action |
|---|---|---|
| Refund processing | Dollar limit, approval above limit | `issue_refund(amount)` over limit |
| Funds transfer | Limit, allowed counterparties, daily cap | Transfer to disallowed party or over cap |
| Data access | Resource scope, purpose limitation | Read of out-of-scope records |
| Scheduling / calendar | Allowed attendees, time window, no external invites | Invite sent externally |
| Record deletion | Allowed resource set, confirmation required | Deletion without confirmation |
| Procurement | Budget cap, approved vendor list | Order over budget or non-approved vendor |

### 13.3 Mutation Operators

Each operator is a registered plugin with a documented definition and a ground-truth effect.

| Operator | Effect | Example |
|---|---|---|
| `NUMERIC_LOOSEN` | Limit raised or removed | "$100" → "$1000" / dropped |
| `QUALIFIER_DELETE` | Condition removed | "unless approved" dropped |
| `NEGATION_FLIP` | Prohibition becomes permission | "do not exceed" → "may exceed" |
| `SCOPE_WIDEN` | Resource or recipient set expands | "this order" → "customer's orders" |
| `SUMMARIZATION_LOSS` | Constraint omitted in compression | long task summarized without limit |
| `STALE_CONTEXT` | Constraint valid at issue, outdated at consumption | long dwell, changed conditions |
| `CONSTRAINT_DROP_ONE` | One of several constraints lost | org cap lost, user limit retained |
| `INJECTED_INSTRUCTION` | Tool output or injected text overrides the limit | "ignore previous limits" in a tool result |
| `NONE` (control) | No mutation | Preserves meaning through paraphrase |

### 13.4 Async Profiles

Each chain carries a profile describing its transport behavior: queue dwell time (immediate, minutes, hours), reordering (none, swapped pair), redelivery (none, duplicate), and fan-in joins. Profiles are what make this an *asynchronous* benchmark rather than a re-labeled multi-turn one.

### 13.5 Record Format (JSONL)

```json
{
  "chain_id": "dcb-000412",
  "split": "test",
  "scenario": "refund",
  "topology": "linear",
  "constraints": [{"id": "c1", "scope": "user", "canonical_text": "..."}],
  "hops": [
    {"hop": 1, "agent": "intake",  "text": "...", "action": null,
     "queue_dwell_s": 0,   "delivery": 1},
    {"hop": 2, "agent": "triage",  "text": "...", "action": null,
     "queue_dwell_s": 720, "delivery": 1},
    {"hop": 3, "agent": "payment", "text": "...",
     "action": {"tool": "issue_refund", "args": {"amount": 2000}},
     "queue_dwell_s": 5,   "delivery": 2}
  ],
  "label": {
    "drift": true,
    "mutation_op": "SUMMARIZATION_LOSS",
    "mutated_hop": 2,
    "violation_at_hop": 3,
    "violated_constraints": ["c1"]
  },
  "provenance": {"generator_version": "0.1.0", "seed": 7, "llm_paraphrase_model": "..."}
}
```

### 13.6 Quality Control

1. **Seeded generation.** Every chain is reproducible from `(generator_version, seed)`.
2. **Hand validation.** A stratified random sample (target: at least 300 chains, covering every operator and scenario) is manually labeled blind to the programmatic label; inter-label agreement is reported. Disagreements are inspected and either fix the generator or are excluded and documented.
3. **Control balance.** Roughly half of chains are `NONE` controls whose paraphrases preserve meaning, including *hard negatives* (heavy rewording that still entails the constraint). These measure false blocks.
4. **Leakage control.** Train/validation/test splits are partitioned by scenario-instance and paraphrase seed so threshold tuning cannot overfit templates.
5. **Dataset card.** Contents, generation procedure, known limitations (synthetic text, template coverage), intended use, and license.

### 13.7 Splits

| Split | Share | Use |
|---|---|---|
| `dev` | 20% | Developing operators and prompts |
| `val` | 20% | Tuning `θ_low`, `θ_high`, `δ_min`, dwell thresholds |
| `test` | 60% | Final reported numbers; touched once per configuration after thresholds are frozen |

Target size: 3,000 to 5,000 chains, which is small enough that Tier 2 evaluation remains affordable.

---

## 14. Evaluation Design

### 14.1 Research Questions

| RQ | Question |
|---|---|
| RQ1 | How accurately does ConstraintLens detect semantic drift and action violations compared with structural and one-shot baselines? |
| RQ2 | What does the false-block rate look like on meaning-preserving rewrites? |
| RQ3 | How much does each tier contribute (accuracy, cost, latency)? |
| RQ4 | How accurately does attribution identify the culprit hop? |
| RQ5 | Is detection robust under asynchronous conditions (long dwell, reordering, redelivery)? |
| RQ6 | Does composition detect the loss of one of several constraints? |
| RQ7 | What overhead does the system add? |

### 14.2 Configurations Compared

| ID | Configuration | Description |
|---|---|---|
| B0 | No check | Pipeline runs unprotected |
| B1 | Presence-only | Constraint object present and hash intact (structural integrity) |
| B2 | One-shot gate | Single semantic check at the first hop only |
| B3 | Tool-arg check only | Tier 1a only: structured comparison at the tool call |
| C1 | ConstraintLens, no Tier 2 | Tiers 0 to 1c |
| C2 | ConstraintLens, full | Tiers 0 to 2 with cache |
| C3 | Ablations | Remove one component at a time: lexical (1b), NLI (1c), judge (2), async guard, composition |

An additional **embedding-similarity-only** configuration is included deliberately to *demonstrate* its weakness on numeric and negation mutations, supporting the design note in Section 7.4.

### 14.3 Metrics

| Metric | Definition |
|---|---|
| Detection recall | Fraction of drifted chains flagged at or before the harmful action |
| Violation prevention rate | Fraction of harmful actions blocked before execution |
| False-block rate | Fraction of control chains where a legitimate action was blocked |
| Precision, F1 | Standard, computed per operator and overall |
| Detection lag | Hops between mutation and first flag (lower is better) |
| Attribution accuracy | Predicted culprit hop equals labeled hop; also top-1 agent accuracy |
| Composition recall | `CONSTRAINT_DROP_ONE` chains where the *specific* dropped constraint is named |
| Added latency | p50 / p95 / p99 per tier and end-to-end |
| Judge usage | Fraction of checks escalated to Tier 2; cache hit rate; estimated cost per 1,000 checks |
| Degraded-run behavior | Metrics when the judge is disabled or fails |

Per-operator breakdowns are mandatory, since aggregate numbers hide the cases where lexical and embedding methods fail.

### 14.4 Protocol

1. Freeze generator and benchmark version.
2. Tune thresholds on `val` only; record chosen values and the objective.
3. Freeze thresholds; run every configuration on `test` with fixed seeds.
4. Repeat stochastic components (judge sampling, if non-zero temperature) with multiple seeds; report mean and confidence intervals (bootstrap).
5. Run async stress sets (long dwell, reordering, redelivery) as separate reported slices.
6. Run **live pipeline trials**: the LangGraph and Redis-worker pipelines execute end to end with real agent LLM calls on a smaller sample, to confirm that results on replayed benchmark chains transfer to live behavior.
7. Archive raw verdicts and event logs so every table in the report is reproducible from `make eval`.

### 14.5 Threats to Validity

| Threat | Mitigation |
|---|---|
| Synthetic data may not reflect real agent rewrites | Live-pipeline trials with real LLM agents; hand-validated sample; limitations stated in dataset card |
| Judge and generator share a model family | Use a different model for paraphrase generation than for judging; report both |
| Threshold overfitting | Tune on `val`, report on `test`, split by scenario instance |
| Label noise | Blind hand validation with agreement reporting |
| Cost of Tier 2 limits scale | Cache, sampling, and an explicit cost-accuracy table |

---

## 15. Dashboard

A React + TypeScript (Vite) single-page app that consumes the WebSocket stream and REST API. Its purpose is to make the invisible visible during demonstration and to present evaluation results.

### 15.1 Views

| View | Content |
|---|---|
| **Live run** | Pipeline graph (React Flow). Agents light up as the task passes. The constraint chip rides along the edge. Each checkpoint shows a green/amber/red badge as verdicts arrive. |
| **Side-by-side** | The same scenario executed through an unprotected pipeline (left) and a protected one (right). The unprotected side lets the violation execute; the protected side blocks it and shows the culprit hop highlighted. |
| **Trace inspector** | Select any past trace: event timeline, per-hop text diff against the original constraint, entailment-score sparkline per constraint, attribution card. |
| **Async view** | Queue dwell time per edge, redeliveries, sequence gaps, staleness indicators. |
| **Results** | Evaluation tables and charts (Recharts): recall by operator, false-block rate, tier usage, latency distribution, attribution accuracy. |
| **Scenario runner** | Buttons to launch scenarios with a chosen mutation operator and hop, which is the control surface for the live demo. |

### 15.2 Design Notes

- State is a pure projection of the event stream; reconnect resumes from `after_event_id`.
- The text diff view highlights the *words that lost the constraint* (limit token missing, negation flipped) using the lexical extractor's spans, so the audience sees *why* drift was flagged.
- Tailwind for styling; no component library lock-in beyond React Flow and Recharts.

---

## 16. CLI

Typer-based; thin wrapper over the same REST API and application use cases.

```
constraintlens serve                          # start the service
constraintlens run scenarios/refund.yaml --mutation SUMMARIZATION_LOSS --hop 2
constraintlens trace show <trace_id>
constraintlens trace replay <trace_id> --theta-low 0.35 --theta-high 0.80
constraintlens bench generate --version 0.1.0 --seed 7 --size 4000
constraintlens bench validate --sample 300
constraintlens eval --config C2 --split test --seeds 5
constraintlens report --run <run_id> --out report/
```

The CLI is intentionally small. Every command maps onto an application use case, so there is no logic in the CLI layer.

---

## 17. Repository Layout

```
constraintlens/
├── README.md
├── ARCHITECTURE.md                 # this document
├── pyproject.toml                  # uv-managed; single workspace
├── Makefile                        # make setup | test | eval | bench | demo
├── docker-compose.yml
├── .env.example
├── .github/workflows/ci.yml
│
├── src/constraintlens/
│   ├── domain/                     # PURE: no framework or I/O imports
│   │   ├── models.py               # Constraint, Envelope, Verdict, Event
│   │   ├── ports.py                # Judge, EventStore, QueueTransport, ...
│   │   ├── checker/
│   │   │   ├── pipeline.py         # TieredChecker (tier registry + routing)
│   │   │   ├── tier0_integrity.py
│   │   │   ├── tier1a_action.py
│   │   │   ├── tier1b_lexical.py
│   │   │   ├── tier1c_nli.py       # depends on Embedder/NLI port, not a library
│   │   │   └── tier2_judge.py
│   │   ├── async_guard.py          # staleness, ordering, loss, redelivery
│   │   ├── composition.py          # effective constraint, dropped detection
│   │   ├── attribution.py          # trace DAG, edge deltas, culprit
│   │   ├── enforcement.py          # decision table, fail policy
│   │   └── extraction.py           # rules + LLM extraction orchestration
│   │
│   ├── application/                # use cases (orchestration)
│   │   ├── open_trace.py
│   │   ├── verify_checkpoint.py
│   │   ├── attribute_trace.py
│   │   ├── replay_trace.py
│   │   └── run_evaluation.py
│   │
│   ├── adapters/
│   │   ├── judge/                  # anthropic.py, openai_compat.py, scripted.py
│   │   ├── store/                  # sqlite.py, memory.py
│   │   ├── transport/              # redis_streams.py, memory.py
│   │   ├── pipeline/               # langgraph.py, redis_worker.py
│   │   └── models/                 # sentence_transformers.py, nli.py
│   │
│   ├── api/                        # FastAPI delivery layer
│   │   ├── app.py                  # composition root (dependency wiring)
│   │   ├── routes/                 # traces, checkpoints, metrics, eval, stream
│   │   └── errors.py
│   ├── sdk/                        # client library used by pipelines
│   ├── cli.py                      # Typer entrypoint
│   └── config.py                   # pydantic-settings
│
├── driftchain_bench/               # benchmark package (separately releasable)
│   ├── scenarios/                  # refund.yaml, transfer.yaml, ...
│   ├── operators/                  # one module per mutation operator
│   ├── generator.py
│   ├── validate.py                 # blind-labeling tooling, agreement stats
│   ├── schema.json                 # JSON Schema for the record format
│   ├── DATASET_CARD.md
│   └── data/                       # generated JSONL (versioned, gitignored if large)
│
├── pipelines/                      # reference pipelines used for demo and live trials
│   ├── refund_langgraph.py
│   ├── triage_langgraph.py
│   ├── procurement_redis_workers.py
│   └── baseline/                   # identical pipelines with no ConstraintLens
│
├── eval/
│   ├── configs/                    # B0..C3 configuration definitions
│   ├── metrics.py
│   ├── stats.py                    # bootstrap CIs
│   └── reports/                    # generated tables and figures
│
├── dashboard/                      # React + TS + Vite
│   └── src/ (views/, components/, hooks/useEventStream.ts, api/)
│
├── tests/
│   ├── unit/                       # domain with fake adapters
│   ├── contract/                   # port contract tests run against every adapter
│   ├── integration/                # service + Redis + SQLite (docker)
│   ├── property/                   # hypothesis-based invariants
│   └── e2e/                        # scenario runs end to end
│
└── docs/
    ├── related_work.md
    ├── threat_model.md
    ├── decisions/                  # ADRs, one file per decision
    └── judge_prompts/              # versioned prompt files
```

**Import rule, enforced in CI** (import-linter): `domain` imports nothing from `adapters`, `api`, `application`, or any third-party I/O library. `application` imports only `domain`. `adapters` and `api` may import `domain` and `application`.

---

## 18. Configuration

Pydantic-settings; values come from environment variables with a `.env` file for local use. Every evaluation run records the effective configuration (with secrets redacted) in its output for reproducibility.

```
# Service
CL_HOST=0.0.0.0
CL_PORT=8000
CL_LOG_LEVEL=INFO

# Storage / transport
CL_DB_URL=sqlite:///./data/constraintlens.db
CL_REDIS_URL=redis://localhost:6379/0

# Checker thresholds (tuned on benchmark val split)
CL_THETA_LOW=0.35
CL_THETA_HIGH=0.80
CL_DELTA_MIN=0.15
CL_DWELL_THRESHOLD_S=300
CL_DEFAULT_MAX_AGE_S=3600

# Tiers
CL_ENABLE_TIER_1C=true
CL_ENABLE_TIER_2=true
CL_NLI_MODEL=<local NLI cross-encoder identifier>
CL_EMBED_MODEL=<local sentence-transformers identifier>

# Judge (provider-agnostic)
CL_JUDGE_PROVIDER=anthropic          # anthropic | openai_compat
CL_JUDGE_MODEL=<model identifier>
CL_JUDGE_API_KEY=...
CL_JUDGE_TIMEOUT_S=10
CL_JUDGE_CACHE=true
CL_JUDGE_PROMPT_VERSION=v1

# Failure policy and enforcement
CL_FAIL_HIGH_RISK=closed
CL_FAIL_LOW_RISK=open
CL_MAX_REGROUNDS=2

# Privacy
CL_STORE_FULL_TEXT=false
CL_REDACT_PATTERNS=email,card,phone
```

Defaults shown for thresholds are starting points only; the reported values come from the tuning procedure in Section 14.4. Model identifiers are deliberately left as configuration, since the judge and paraphrase models are chosen at evaluation time.

---

## 19. Deployment

### 19.1 Local / Demo

```
docker compose up
```

| Service | Role |
|---|---|
| `redis` | Streams transport |
| `constraintlens` | FastAPI service (+ model weights baked or mounted) |
| `pipelines` | Reference pipelines and workers |
| `dashboard` | Built static app served by nginx |

Volumes: `./data` (SQLite, benchmark data), `./models` (cached local models). A `make demo` target starts the stack, seeds the demo scenarios, and opens the dashboard.

### 19.2 Development

`uv sync` installs dependencies. `make test` runs unit, contract, and property tests without Docker. `make integration` brings up Redis in a container and runs the integration suite. Pre-commit hooks run formatting (ruff), typing (mypy strict on `domain`), and import-linter.

### 19.3 CI (GitHub Actions)

Pipeline stages: lint and type-check, unit + contract + property tests, integration tests with a Redis service container, benchmark schema validation, build of dashboard, and a small deterministic smoke evaluation using the `ScriptedJudge` (no API calls in CI).

### 19.4 Model and Cost Management

- Local models (embedder, NLI) run on CPU; GPU is optional.
- The LLM judge is used behind the cache. An evaluation run reports API call counts and estimated cost; a `--max-judge-calls` guard prevents runaway spend.
- Benchmark paraphrase generation is a one-time batch job whose outputs are stored; evaluation never regenerates text.

---

## 20. Testing Strategy

| Level | What it covers | Notes |
|---|---|---|
| **Unit** | Domain logic with fake adapters: each tier, composition rules, attribution, decision table, async guard | No network, deterministic, fast |
| **Contract** | One test suite per port, run against *every* adapter (e.g., `EventStore` against memory and SQLite; `Judge` against scripted and real in an opt-in mode) | Guarantees adapters are true substitutes (Liskov) |
| **Property-based** (Hypothesis) | Invariants: verdicts are idempotent under any redelivery order; effective constraint is never looser than any input constraint; reordering never lets a later hop's state be overwritten; replaying events reproduces projections exactly | Catches the async edge cases example tests miss |
| **Mutation-operator tests** | Each operator produces its documented effect and label; control (`NONE`) preserves entailment per a reference check | Protects benchmark validity |
| **Integration** | Service + Redis Streams + SQLite: delivery, redelivery, pending reclaim, timeouts | Docker-based |
| **End-to-end** | Reference pipelines run a scenario with a mutation; assert block, culprit hop, event log contents, and WebSocket output | Includes the unprotected-baseline comparison |
| **Performance** | Latency budget assertions per tier; throughput under concurrent checkpoints | Fails CI on budget regression for Tiers 0 to 1b |
| **Failure injection** | Judge timeout, malformed judge output, store outage, service unreachable, Redis restart mid-trace | Verifies the fail policy table |

Coverage targets: 90% line coverage on `domain/`; every row of the decision table in Section 7.8 has at least one test.

---

## 21. Security and Privacy

### 21.1 Threat Model (summary)

| Threat | Relevance | Mitigation |
|---|---|---|
| **Prompt injection altering task text** | Direct cause of the `INJECTED_INSTRUCTION` drift class | Action gate (Tier 1a) is independent of text; judge prompt treats task text strictly as data and uses delimiters and a fixed output schema |
| **Judge manipulation** (text crafted to coax "ENTAILED") | Task text is attacker-influenced | Structured-argument check is authoritative for action violations; judge verdicts require a quoted evidence span from the constraint; adversarial cases included in the benchmark |
| **Envelope tampering or stripping** | Constraint loss in transit | Service-held authoritative constraint set; content hash and envelope digest checks |
| **Replay / duplicate delivery** | At-least-once transport | Idempotent verdicts keyed on `(trace_id, hop_id, checkpoint)` |
| **Service unavailability used to bypass checks** | Fail-open risk | Fail-closed default for high-risk; SDK circuit breaker |
| **Data exposure in logs** | Task text may hold PII | Redaction before persistence; text storage opt-in |
| **API abuse** | Service exposes verification endpoints | API-key authentication on the service, rate limiting, request size limits |

A fuller `docs/threat_model.md` is maintained alongside the code.

### 21.2 Explicit Non-Guarantees

ConstraintLens reduces silent constraint loss; it does not provide formal verification. A sufficiently adversarial agent that controls both text and structured arguments consistently within the allowed range can pass the checks. Cryptographic delegation integrity is out of scope and complementary.

### 21.3 Secrets

Judge API keys are read from the environment, never logged, never written to the event log, and redacted in recorded configuration snapshots.

---

## 22. Observability

ConstraintLens must itself be observable, since it is a safety component.

| Signal | Detail |
|---|---|
| **Structured logs** | JSON logs with `trace_id`, `hop_id`, `checkpoint`, `tier`, `decision`, `latency_ms` |
| **Metrics** | Counters and histograms for checkpoints by decision, tier reached, escalation rate, cache hit rate, judge failures, degraded verdict rate, latency per tier (Prometheus-format `/metrics` endpoint) |
| **Health** | `/v1/health` reports store, Redis, judge reachability, and model load status |
| **Self-audit** | A scheduled job verifies that every `PUBLISH` has a matching `CONSUME` or a recorded timeout, and that projections match a fresh rebuild from the log |
| **Alerts (demo-level)** | Dashboard banner when degraded-verdict rate or judge failure rate exceeds a configured level |

---

## 23. Implementation Roadmap

Planned for a **16-week** schedule (a 3 to 5 month window; compress or extend phases 4 to 6 as needed). Each phase ends with a demonstrable, tested increment. Phases are ordered so that the parts the research claim depends on (async boundary, tiered checker, evaluation) are built before presentation work.

| Phase | Weeks | Deliverable | Exit criteria |
|---|---|---|---|
| **0. Foundations** | 1 | Repo, tooling, CI, ADR template; domain models and ports; fakes | `make test` green; import-linter passes; models validate fixtures |
| **1. Constraint layer** | 2 to 3 | Schema, rule + LLM extraction, canonicalization, hashing; composition rules | Extraction accuracy measured on a hand-written set of 100 instructions; composition property tests pass |
| **2. Baseline pipelines** | 3 to 4 | Refund pipeline in LangGraph and a Redis-worker pipeline, both *without* ConstraintLens; baseline versions of the unprotected demo | Pipelines run end to end; reproducing the silent violation on demand |
| **3. Service + direct handoff** | 4 to 6 | FastAPI service, SDK, event store, Tier 0 and 1a, enforcement, direct-handoff checkpoints | Action violations blocked in the LangGraph pipeline; event log complete and replayable |
| **4. Async boundary** | 6 to 8 | Redis Streams transport, async guard (staleness, reordering, loss, redelivery), idempotent verdicts, `PUBLISH` / `CONSUME` checkpoints | Property tests for ordering and idempotency pass; failure-injection suite passes |
| **5. Semantic tiers** | 8 to 10 | Tier 1b lexical, Tier 1c NLI, Tier 2 judge with cache, routing and uncertainty band | Per-tier latency within budget; judge failure path degrades correctly |
| **6. Benchmark** | 9 to 12 | Generator, operators, scenario templates, paraphrase batch, hand validation, dataset card | 3,000+ chains; validation sample labeled; agreement reported; schema locked |
| **7. Attribution** | 11 to 12 | DAG attribution, async-cause classification, fan-in handling | Attribution accuracy measured on `val`; replay endpoint works |
| **8. Evaluation** | 12 to 14 | Threshold tuning on `val`; all configurations on `test`; ablations; async slices; live-pipeline trials; stats | All tables and figures reproducible via `make eval` |
| **9. Dashboard + CLI** | 13 to 15 | Live view, side-by-side, trace inspector, results view; CLI | Full demo scenario runs from one command |
| **10. Hardening and write-up** | 15 to 16 | Second-adapter polish, docs, threat model, dataset release, demo script, final report | Clean-clone `docker compose up` works; report complete |

**Critical path:** Phase 3 → 4 → 5 → 8. The benchmark (Phase 6) runs in parallel with Phases 4 and 5 and must be locked before threshold tuning.

**Scope valves** (cut in this order if time compresses): dashboard polish → second framework adapter breadth → adversarial operator variants → attribution fan-in refinements. Never cut: the tool-call gate, the async guard, the benchmark validation sample, or the held-out test discipline.

**Demo script (about 3 minutes):**
1. Show the unprotected refund pipeline silently issuing a $2,000 refund against a $100 limit.
2. Run the same scenario with ConstraintLens. Watch the constraint chip travel; show the red flag at the triage hop and the block at the tool call.
3. Open the trace inspector: highlight the lost limit in the text diff, show the attribution card naming the hop.
4. Show the async view: a 12-minute queue dwell and redelivery handled without a conflicting verdict.
5. Show the results view: recall by operator, false-block rate, latency, and the embedding-only failure case.

---

## 24. Risks and Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Semantic tiers produce too many false blocks or misses | Medium-high | High | Structured-argument check as authoritative for actions; hard-negative controls in benchmark; uncertainty band tuned on `val`; judge only for the band |
| R2 | Synthetic benchmark criticized as unrealistic | Medium | Medium | Hand-validated sample; live-pipeline trials with real agents; limitations stated openly |
| R3 | LLM judge cost or rate limits during evaluation | Medium | Medium | Cache, `--max-judge-calls`, cost-accuracy table, evaluation on `test` only after thresholds frozen |
| R4 | Judge shares blind spots with the paraphrase generator | Medium | Medium | Different models for generation and judging; report both |
| R5 | Async edge cases (at-least-once, fan-in) cause subtle bugs | Medium | High | Property-based tests; failure-injection suite; idempotency by construction |
| R6 | Scope creep from additions (attribution, composition, benchmark) | High | High | Roadmap scope valves; each addition has its own exit criterion; YAGNI on ports |
| R7 | Novelty claims for benchmark, attribution, or composition challenged | Medium | Medium | Complete the prior-art check (Section 25) before claiming novelty; frame these as contributions *applied to async delegation*, not as inventions of the underlying techniques |
| R8 | Framework-agnostic claim unsupported | Medium | Medium | Two real adapters (LangGraph, plain Redis workers) exercised by the same contract tests |
| R9 | Name or package collision (`constraintlens`) | Low-medium | Low | Search GitHub and PyPI before public release; rename is mechanical thanks to a single package root |
| R10 | Latency budget exceeded in Tier 1c | Low-medium | Medium | Small NLI model; CPU benchmarking early in Phase 5; fallback to Tier 2 routing |

---

## 25. Related Work and Positioning

### 25.1 Positioning

| Line of work | What it addresses | Gap ConstraintLens targets |
|---|---|---|
| Delegated-authorization and scoped-token schemes for agents (e.g., Cisco's ASTRA work on semantic task-to-scope matching) | One-shot semantic gating of an access request; minimal scopes | No *continuous* re-verification across hops; no asynchronous time-gap handling |
| Constraint drift position paper (University of Liverpool) | Names constraint drift and five diagnostic modes (memory, authority, information-flow, accountability, utility-induced) | Position paper without an implementation or benchmark; ConstraintLens operationalizes detection and attribution |
| DRIFTBENCH | Constraint drift in single-agent, multi-turn ideation | Not multi-agent delegation, not asynchronous |
| Multi-agent safety and trust-scoring research (e.g., cascade and hallucination-propagation detection) | Error propagation modeled over synchronous agent graphs | Assumes synchronous, structured communication; async queue boundaries are not modeled |
| Agent observability and tracing | Spans and parent-child structure | Event-driven handoffs break the parent-child model; guardrail state is not traced |
| Prompt-injection and guardrail products | Input/output filtering for single agents | Not constraint-state-aware across a delegation chain |

### 25.2 Claims, Stated Carefully

- **Claimed:** a runtime system that verifies *semantic* preservation of constraints continuously across *asynchronous* delegation hops, enforced at a tool-call gate, with an event-sourced audit trail.
- **Claimed as contributions in this setting:** hop-level drift attribution, multi-constraint composition with dropped-constraint detection, and a labeled benchmark of asynchronous delegation chains.
- **Not claimed:** novelty of NLI, LLM-as-judge, process-style attribution, or most-restrictive-wins resolution as techniques. They are applied here to a problem setting where they were not previously combined.

### 25.3 Verification Status of Sources

| Source | Status |
|---|---|
| Cisco ASTRA (semantic task-to-scope matching) | Verified to exist; characterization as one-shot gating checked |
| Liverpool "Constraint Drift" position paper | Verified to exist; summary checked |
| DRIFTBENCH | Verified to exist; scope (single-agent multi-turn) checked |
| Vouch Protocol, HDP, IETF draft, "Five-Plane Reference Architecture" | **Not yet verified.** Retrieve primary sources and confirm claims before citing in the final report |
| Prior art for async-constraint benchmarks, drift attribution, and constraint composition | **Not yet searched.** Must be completed before the novelty statements in the report are finalized |

Full citations, with links and access dates, belong in `docs/related_work.md`.

---

## 26. Glossary

| Term | Meaning |
|---|---|
| **Constraint** | A structured restriction attached to a task (limit, approval requirement, scope, expiry) |
| **Constraint set (𝒞)** | All constraints attached to a trace |
| **Textual drift** | The task text no longer entails a constraint |
| **Action violation** | A proposed tool call breaks a constraint, regardless of text |
| **Hop** | One agent step; the unit of delegation and of attribution |
| **Checkpoint** | A point where the SDK asks the service for a verdict: `PUBLISH`, `CONSUME`, `TOOL_CALL` |
| **Envelope** | Metadata structure carrying constraints and hop identity alongside the task payload |
| **Tier** | One stage of the checker, ordered by cost: 0 deterministic, 1a/1b/1c cheap semantic, 2 LLM judge |
| **Uncertainty band** | The entailment-score interval `[θ_low, θ_high]` that triggers escalation to Tier 2 |
| **Re-ground** | Re-inject the explicit constraint text into an agent's context and retry |
| **Culprit hop** | The hop identified by attribution as having introduced a failure |
| **Event log** | Append-only record from which all state and views are derived |
| **Degraded verdict** | A verdict issued while a tier was unavailable or failed |
| **driftchain-bench** | The released benchmark of labeled asynchronous delegation chains |
| **Port / adapter** | Interface the domain depends on / concrete technology implementing it |

---

*End of document.*
