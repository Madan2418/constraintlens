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
| 2. Baseline pipelines | next |
| 3–10 | planned |

Extraction accuracy (rules only, `make extraction-eval`): 1.00 exact match on the
100-instruction gold set, which is in-sample (same author as the rules), and 0.85 on a
20-instruction probe set written after the rules were frozen, with precision 1.0. All
misses are empty outputs, which trigger the LLM fallback. See `eval/reports/`.

## Quick start (development)

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync            # install runtime + dev dependencies
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
  api/ sdk/      delivery layer and client SDK (Phase 3)
  config.py      CL_* environment settings (§18)
tests/
  unit/ contract/ property/   (integration/ and e2e/ arrive with Phases 3–4)
docs/decisions/  architecture decision records
```

The import rule in §17 is enforced by import-linter: `domain` imports no other layer
and no framework or I/O library, and `application` imports only `domain`.
