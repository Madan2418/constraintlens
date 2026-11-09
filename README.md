# ConstraintLens

**A runtime framework for detecting semantic constraint drift in asynchronous multi-agent AI systems.**

When a task like *"refund up to $100, approval above"* is delegated across agents and
message queues, each agent re-expresses it in its own words. The limit can be lost along
the way, even though every structural check still passes. ConstraintLens checks at every
delegation boundary and at the tool-call gate that the constraint's *meaning* survived. It
names the hop that lost it and blocks the drifted action before it runs.

The full design is in [`ARCHITECTURE.md`](ARCHITECTURE.md). Supporting material (literature
survey, review decks, diagrams) is in [`documentation/`](documentation/).

## Status

| Phase (ARCHITECTURE.md §23) | State |
|---|---|
| 0. Foundations: repo, tooling, CI, ADRs, domain models, ports, fakes | **done** |
| 1. Constraint layer: extraction, canonicalization, hashing, composition | **done** |
| 2. Baseline pipelines: unprotected LangGraph refund + Redis-worker procurement | **done** |
| 3. Service + direct handoff: FastAPI `/v1`, SDK, SQLite event log, Tier 0 + 1a, enforcement | **done** |
| 4. Async boundary | next |
| 5–10 | planned |

Extraction accuracy (rules only, `make extraction-eval`): 1.00 exact match on the
100-instruction gold set, which is in-sample (same author as the rules), and 0.85 on a
20-instruction probe set written after the rules were frozen, with precision 1.0. All
misses are empty outputs, which trigger the LLM fallback. See `eval/reports/`.

## See the problem (Phase 2)

```bash
make demo-baseline
# or one run at a time:
uv run python -m pipelines.baseline.refund_langgraph --mutation SUMMARIZATION_LOSS --hop 2
uv run python -m pipelines.baseline.refund_langgraph --matrix --org-policy
uv run python -m pipelines.baseline.procurement_redis_workers --crash-before-ack
```

The first command reproduces ARCHITECTURE.md §2.1. Triage drops the $100 limit, payments
refunds $2,000, and nothing in the pipeline notices. The Redis pipeline uses in-process
fakeredis unless `--redis-url` / `REDIS_URL` points at a real server.

## See ConstraintLens block it (Phase 3)

```bash
make demo-protected
# or:
uv run python -m pipelines.refund_langgraph --mutation SUMMARIZATION_LOSS --hop 2 --compare
uv run python -m pipelines.refund_langgraph --matrix --org-policy
```

The same agents and the same drift: the baseline silently refunds $2,000, while the protected
pipeline is stopped at the tool gate (`issue_refund` never runs). Every checkpoint verdict
lands in the event log and can be replayed. By default the service runs in-process with
an in-memory store. To use a real server:

```bash
uv run python -m constraintlens.api      # http://localhost:8000/docs  (CL_* settings)
uv run python -m pipelines.refund_langgraph --mutation NEGATION_FLIP --hop 2 --base-url http://localhost:8000
```

## Quick start (development)

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras   # runtime, dev and pipeline dependencies
make test          # unit, contract and property tests (no Docker needed)
make lint          # ruff, mypy (strict on domain/), import-linter
```

On Windows without `make`, run the commands from the `Makefile` directly, e.g.
`uv run pytest` and `uv run lint-imports`.

## Layout

```
src/constraintlens/
  domain/        pure core: models, ports, (later) checker tiers, guard, attribution
  application/   use cases (Phase 1+)
  adapters/      port implementations; fakes used by tests live here too
  api/           FastAPI delivery layer; wiring.py is the composition root
  sdk/           client used by pipelines: guard_hop, guard_tool, circuit breaker
  config.py      CL_* environment settings (§18)
pipelines/
  common/        scripted agents, scenarios, oracle (never imports ConstraintLens)
  baseline/      unprotected LangGraph and Redis-worker pipelines
  refund_langgraph.py   the same refund pipeline, protected by the SDK
tests/
  unit/ contract/ property/   (integration/ and e2e/ arrive with Phases 3–4)
docs/decisions/  architecture decision records
```

The import rule in §17 is enforced by import-linter: `domain` imports no other layer
and no framework or I/O library, and `application` imports only `domain`.
