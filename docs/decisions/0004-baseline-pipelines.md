# ADR 0004: Baseline reference pipelines (Phase 2)

- **Status:** accepted
- **Date:** 2026-11-07
- **Spec reference:** ARCHITECTURE.md §2.1, §13.2–13.4, §17 (`pipelines/`), §23 Phase 2

## Context

Phase 2 needs a LangGraph refund pipeline and a Redis-worker pipeline, both *without*
ConstraintLens, that reproduce the silent violation on demand. The machine has no Redis
or Docker, and the demo and CI must be deterministic and free of API calls.

## Decision

1. **Scripted, deterministic agents** (`pipelines/common/`). Each agent reads the text it
   receives into task facts and writes its own text from them in its own words. Drift is
   injected by applying a §13.3 mutation operator at a chosen hop. Downstream agents see
   only text, so lost meaning stays lost. That is the real failure mechanism, and it makes
   runs reproducible from `(scenario, mutation, hop, org_policy)`. Real-LLM agents (needed
   for the §14.4 live trials) plug in later behind the same `agent_step` signature.
2. **Operators**: NONE, NUMERIC_LOOSEN, QUALIFIER_DELETE, NEGATION_FLIP, SCOPE_WIDEN,
   SUMMARIZATION_LOSS, INJECTED_INSTRUCTION (through a tool result), and
   CONSTRAINT_DROP_ONE (needs the org policy). STALE_CONTEXT depends on the async boundary
   and comes with Phase 4. These are scenario-specific scripts, not the generic,
   registered benchmark operators of Phase 6.
3. **An independent oracle** judges executed tool calls against the user's *original*
   intent. It lives outside the pipeline and does not use ConstraintLens, so it can later
   score ConstraintLens fairly.
4. **Scenarios**: refund in LangGraph (exactly §2.1, with a 12-minute dwell on a virtual
   clock) and procurement on Redis Streams workers (§17 names
   `procurement_redis_workers.py`; 45-minute dwell). The Redis pipeline uses real
   consumer-group semantics (XREADGROUP / XACK / XAUTOCLAIM). Its `--crash-before-ack`
   mode shows at-least-once redelivery executing an order twice, the problem §9.6 exists
   to solve.
5. **Redis**: the redis-py client runs against a real server when `--redis-url` /
   `REDIS_URL` is set, and against in-process **fakeredis** otherwise (demo, CI). A
   real-Redis test is marked `integration` and skipped without `REDIS_URL`.
6. **Enforced "unprotected"**: an import-linter contract forbids `pipelines.baseline`
   and `pipelines.common` from importing `constraintlens`. Protected versions
   (`pipelines/refund_langgraph.py` etc., Phases 3–4) will reuse `pipelines.common`
   unchanged, so the side-by-side comparison differs only by ConstraintLens.
7. Pipeline dependencies are an optional extra (`pipelines`: langgraph, redis), so the
   service does not depend on them. `make setup` and CI install all extras.

## Consequences

- Exit criteria are tested in `tests/e2e/test_baseline_pipelines.py`: the full
  mutation × hop × org-policy matrix for both pipelines against a ground-truth table,
  the §2.1 texts word for word, and the redelivery duplicate.
- Some drift causes no harm. QUALIFIER_DELETE in refund, and SCOPE_WIDEN in procurement
  where 60 laptops fit no budget, change the text but not the action. With the org policy
  on, user-limit drift is masked by the stricter org cap. These cases matter for §4.2:
  textual drift and action violation are different failures.
