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
| 4. Async boundary: Redis Streams transport, async guard, idempotent verdicts, Redis-worker adapter | **done** |
| 5. Semantic tiers: T1b lexical, T1c NLI, T2 judge with cache, uncertainty-band routing | **done** |
| 6. Benchmark: driftchain-bench generator, 9 operators, 6 scenarios, 4,000 chains, locked schema, validation tooling | **done** (human validation labels pending) |
| 7. Attribution: DAG culprit hop, async causes, fan-in, `GET .../attribution`, replayed attribution | **done** |
| 8. Evaluation: harness, all §14.2 configs, metrics, bootstrap CIs, figures, provisional val tuning, locked test split | **done on val** (test locked: needs human labels, NLI model, judge key) |
| 9. Dashboard + CLI | next |
| 10 | planned |

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

## The async boundary (Phase 4)

```bash
make demo-async
# or:
uv run python -m pipelines.procurement_redis_workers --crash-before-ack --dwell-minutes 2 --compare
uv run python -m pipelines.procurement_redis_workers          # 45-minute dwell
```

The first run compares the baseline with the protected pipeline when a worker dies after
ordering but before acking. The baseline places the order twice. With ConstraintLens the
redelivered message gets the stored verdicts, and the re-issued `place_order` is
recognised as already authorized, so it runs once. The second run keeps the task in the
queue for 45 minutes. The budget is then stale and must be re-verified by a semantic
tier (§9.3). Those tiers arrive in Phase 5, so the high-risk purchase fails closed for
now. The guard also holds checkpoints whose predecessor is missing, flags late
deliveries, and a sweeper reports messages published but never consumed. See
`docs/decisions/0006-async-boundary.md`.

## Semantic tiers (Phase 5)

Tier 1b (lexical) is always on, so the demos now stop drift at the hop that introduced
it, before any tool runs:

```bash
uv run python -m pipelines.refund_langgraph --matrix --org-policy
uv run python -m eval.latency_budget            # per-tier latency vs the §10.3 budgets
```

Tier 1c and Tier 2 switch on when their models are configured (`/v1/health` lists the
live tiers):

```bash
uv sync --all-extras --group models     # anthropic SDK + sentence-transformers (CPU)
export CL_NLI_MODEL=<cross-encoder NLI model id>   # Tier 1c
export CL_EMBED_MODEL=<sentence-transformers id>   # optional routing
export CL_JUDGE_API_KEY=...                        # Tier 2; CL_JUDGE_MODEL defaults to claude-opus-5-5
```

The judge runs only for NLI-band and stale constraints, behind a cache. Replay never
calls it. Each judge call is billed by the provider, so `CL_ENABLE_TIER_2=false` turns
it off. A judge that fails, times out, refuses or returns invalid output degrades the
verdict, and the fail policy decides. See `docs/decisions/0007-semantic-tiers.md`.

## Benchmark: driftchain-bench (Phase 6)

`driftchain_bench/` is a separately releasable package: it imports nothing from
ConstraintLens, and ConstraintLens never imports it. It generates labeled asynchronous
delegation chains:

- **6 scenarios** (refund, transfer, data access, scheduling, deletion, procurement);
- **8 drift operators** plus `NONE` controls, half of the controls hard negatives;
- **linear, fan-out and fan-in topologies**, with dwell, reordering and redelivery
  profiles.

```bash
uv run python -m driftchain_bench generate --version 0.1.0 --seed 7 --size 4000
uv run python -m driftchain_bench check        # schema lock + byte-for-byte reproduction
uv run python -m driftchain_bench validate --sample 300   # blind hand-validation sample
```

The 4,000-chain release is reproduced from its seed and pinned by the SHA-256 in
`driftchain_bench/manifests/`; the JSONL itself is not committed.

| split | chains |
|---|---|
| dev | 816 |
| val | 816 |
| test | 2,368 |

Labels are computed by construction, and on every chain they agree with ConstraintLens's
own structural action check. **Hand-validation labels are still pending.** The blind
sample, the labeling guide and the agreement tooling are in
`driftchain_bench/validation/`. See `driftchain_bench/DATASET_CARD.md` and
`docs/decisions/0008-driftchain-bench.md`.

## Attribution (Phase 7)

When a trace fails, ConstraintLens names the hop that introduced the failure and why:
`REWRITE`, `INJECTION`, `STALE_CONTEXT` or `TRANSIT_LOSS` (blamed on the queue edge,
not an agent), or `UNKNOWN`. It is a view over the event log:

```bash
curl localhost:8000/v1/traces/<id>/attribution                # computed now
curl "localhost:8000/v1/traces/<id>/attribution?delta_min=0.3"  # other parameters
curl -X POST localhost:8000/v1/traces/<id>/replay -d '{"tiers": ["T0","T1a"]}'  # + replayed attribution
```

Closing a trace records the final attribution (`ATTRIBUTION_COMPUTED`), and the protected
demos print it, e.g. `Attribution: h2 (triage) lost '... 100 USD ...'; cause REWRITE`.

Attribution accuracy on driftchain-bench `val` (`make attribution-eval`; 408 drifted
chains; correct = the labeled mutated hop):

| detector | dwell threshold | hop accuracy |
|---|---|---|
| rules (T0, T1a, T1b), the system on this host | 300 s (default) | 70.8% |
| rules | age bound only | 85.0% |
| oracle text detector (upper bound, not a system result) | age bound only | 100.0% |

The algorithm is exact when detection is. The gap is detection (scope widening and soft
negations need the semantic tiers, which have not run here) and the untuned dwell
threshold. See `eval/reports/attribution_val_v1.md` and
`docs/decisions/0009-drift-attribution.md`.

## Evaluation (Phase 8)

```bash
make bench   # regenerate the benchmark (gitignored JSONL)
make eval    # provisional tuning on val, every runnable config on val, attribution
```

Every §14.2 configuration is defined in `eval/configs`. The ones whose models are
missing (C1/C2 need an NLI model and a judge; EMB needs an embedder) are reported as
*not run*, never substituted. Results on `val` (816 chains; 95% bootstrap CIs; provisional
thresholds):

| config | detection recall | violation prevention | false-block rate |
|---|---|---|---|
| B0 no check | 0.0% | 0.0% | 0.0% |
| B3 tool-arg check only | 55.9% | 82.3% | 0.0% |
| **R** ConstraintLens, rules tiers (T0, T1a, T1b, async guard, composition) | **96.1%** [94.1, 97.8] | **96.4%** [93.9, 98.6] | **8.6%** [5.9, 11.4] |
| oracle text detector (upper bound, not a system) | 100.0% | 96.0% | 0.0% |

The ablations show each component carrying weight: without the async guard,
STALE_CONTEXT recall falls from 100% to 18%; without composition, CONSTRAINT_DROP_ONE
falls from 100% to 9%. The 8.6% false blocks are Tier 1b firing on hard-negative
rewordings; re-verifying them is the semantic tiers' job. Tables, per-operator and async
slices, latency, and figures: `eval/reports/eval_val_v1.md`, `eval/reports/figures/`.

**The `test` split is locked** until thresholds are frozen
(`eval/configs/thresholds.json`, `"status": "frozen"`), which needs the human validation
labels and the semantic tiers. Each configuration can then be run on test once
(`make eval-test`, recorded in `eval/reports/test_ledger.json`). The evaluation also
found and fixed a fan-in idempotency bug (a join's second CONSUME was treated as a
conflicting duplicate). See `docs/decisions/0010-evaluation-harness.md`.

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
  domain/        pure core: models, ports, checker tiers (T0–T2), async guard, enforcement
  application/   use cases (Phase 1+)
  adapters/      port implementations; fakes used by tests live here too
  api/           FastAPI delivery layer; wiring.py is the composition root
  sdk/           client used by pipelines: guard_hop, guard_tool, GuardedWorker, breaker
  config.py      CL_* environment settings (§18)
pipelines/
  common/        scripted agents, scenarios, oracle (never imports ConstraintLens)
  baseline/      unprotected LangGraph and Redis-worker pipelines
  refund_langgraph.py           the refund pipeline, protected by the SDK
  procurement_redis_workers.py  the Redis-worker pipeline, protected (GuardedWorker)
tests/
  unit/ contract/ property/ e2e/   (e2e includes the failure-injection suite)
driftchain_bench/  benchmark: scenarios/*.yaml, operators/, generator, schema.json,
                   manifests/, validation/ (blind sample, guide), DATASET_CARD.md
docs/decisions/  architecture decision records
```

The import rule in §17 is enforced by import-linter: `domain` imports no other layer
and no framework or I/O library, and `application` imports only `domain`.
