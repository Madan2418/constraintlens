# ADR 0003: Constraint layer details (Phase 1)

- **Status:** accepted
- **Date:** 2026-11-07
- **Spec reference:** ARCHITECTURE.md §7.1, §7.2, §7.6, §8.1, §23 Phase 1

## Context

§7.1 and §7.6 state the extraction pipeline and composition rules in one line each.
Implementing them requires choices the spec leaves open, and one rule that, read
literally, breaks the spec's own invariant ("effective constraint is never looser than
any input constraint", §20).

## Decision

### 1. Subject scope grammar (`domain/subject.py`)
`subject` is a set of items: `"*"` (everything), `kind:id`, `kind:*`, or `{a,b,...}`, lower-cased.
That makes "resource scope: intersection" (§7.6) computable. `Constraint` and
`ConstraintSpec` reject malformed subjects. An org-wide policy uses `"*"`, so the Phase 0
fixture's `"refunds"` became `"*"`.

### 2. `ConstraintSpec`
This is a new model: a constraint *before* issue, with no id, issue time, canonical text
or hash. Extraction and declared policies produce specs, and `canonical.issue()` turns them
into `Constraint`s. `Constraint.spec()` is the inverse.

### 3. Canonicalization and hashing (`domain/canonical.py`)
- `canonical_text` is generated from the fields by a fixed template. It is
  deterministic, so the same meaning always gives the same text and hash.
- `content_hash` = SHA-256 of canonical JSON over **every field except the hash**:
  sorted keys, decimals without exponent or trailing zeros, UTC times with microseconds
  and `Z`, lower-case UUIDs. `issue()` normalises values first, so `100.00` and `1E+2`
  hash the same.
- `envelope_digest` = SHA-256 over the trace id plus the sorted `(constraint_id, content_hash)` pairs.

### 4. Composition semantics (`domain/composition.py`)
- Grouping: by `(action, resource)`. The "allowed action set: intersection" rule is
  satisfied *within* a group, because constraints on different actions are independent.
  A "refund" constraint does not forbid transfers.
- **Hard caps and approval thresholds are kept apart.** `requires_approval_above_limit`
  means amounts above the limit are allowed *with approval*. `none` / `always` mean they
  are forbidden. Taking "min limit" and "strictest authority" independently would turn
  an org *hard* cap of 50 plus a user "approval above 100" into "approval above 50",
  which allows amounts the org forbids. So the effective constraint holds
  `hard_max`, `hard_min`, and `approval_above` per unit, plus `approval_always`.
  The invariant is property-tested for random amounts, subjects and times.
- Units are not converted. An amount is comparable only when **every** bound in the
  group uses its unit. Otherwise it is `incomparable`, ranked with `forbidden`
  (fail-safe, §5), unless a hard bound in its own unit already forbids it. The group
  gets an `INCOMPARABLE_UNITS` conflict at trace open. (The first version checked only
  the amount's own unit. The never-looser property test found that this let a USD amount
  pass while an INR cap in the same group went unchecked.)
- `UNSATISFIABLE` = empty subject intersection, or a hard minimum above a hard maximum.
  The §7.6 example ("approval required" vs "no human in the loop") is not representable
  in §8.1's `authority` vocabulary, so it is not detected yet.
- `SUPERSEDED` is reported per dimension (limit, authority, subject, expiry) when a
  lower-precedence constraint admits something a higher one forbids.
- `find_dropped` covers the envelope half of §7.6 dropped-constraint detection. The "as
  entailed by T_k" half needs the semantic tiers (Phase 5). An envelope with no
  constraints is `CONSTRAINT_LOST`, and each missing id is `CONSTRAINT_DROPPED`.

### 5. Extraction (`domain/extraction.py`)
- The rule extractor splits sentences, segments them by action verb, and attaches
  attributes from sentences with no action to the current action. It reads money (symbols,
  ISO codes, words, `k` / `lakh` / `million`) and counts (records, files, attendees,
  accounts, items), then classifies each quantity by the nearest left cue or a right cue.
  It detects approval ("always" vs "above a threshold" vs explicitly not needed), expiry
  (relative windows, ISO dates; a bare date means 23:59:59Z), subject ids, and "only".
- A constraint is emitted only for an explicit restriction: a limit, an approval rule, an
  expiry, or an "only" scope. A bare amount ("Refund order #888 for $40") is *not* treated
  as a limit. A cap plus a separate approval threshold gives two constraints.
- LLM path: `Judge.extract_constraints` returns raw text. The domain validates it
  strictly (`extra="forbid"`, expiry after issue time), retries once with the error as
  `repair_hint`, then rejects (`LLM_REJECTED`, `degraded=true`). Judge failure gives
  `LLM_UNAVAILABLE` and the rules-only result. Prompt: `docs/judge_prompts/extract_v1.md`.
- `llm_mode`: `off` | `fallback` (default; call the LLM only when rules find nothing) | `always`.
  Configured by `CL_EXTRACTION_LLM_MODE`, which is not in §18 and is added here.
- Reconciliation: one-to-one per `(action, resource)` → per field most restrictive
  (min max, max min, strictest authority `always > none > above_limit`, subject
  intersection, earliest expiry), with a review flag on any disagreement. Anything else
  keeps both sides (`AMBIGUOUS_MATCH`), which is never looser, because composition takes
  the strictest of all of them.
- Prohibitions ("never delete anything") and spelled-out numbers are not covered by the
  rules; the LLM fallback handles them.

### 6. Trace open (`application/open_trace.py`)
`OpenTrace` is built now, without the HTTP endpoint (Phase 3). The instruction text is
stored only when `store_full_text` is on; otherwise only its SHA-256 and length are
stored (§12.4).

## Consequences

- Extraction accuracy is reported against `eval/extraction/gold_v1.jsonl` (100
  instructions) and a 20-instruction probe set written after the rules were frozen
  (`probe_v1.jsonl`). Both were written by the same author as the rules; see the report.
- Tier 1a (Phase 3) can use `EffectiveConstraint.evaluate` directly.
