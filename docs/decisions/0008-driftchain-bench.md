# ADR 0008: driftchain-bench (Phase 6)

- **Status:** accepted
- **Date:** 2026-11-10
- **Spec reference:** ARCHITECTURE.md §4.4, §13, §14.5, §16, §17, §19.3, §19.4, §20, §21.1, §23

## Context

Phase 6 delivers the generator, the mutation operators, the scenario templates, the
paraphrase batch, hand validation and the dataset card. Exit criteria: 3,000+ chains;
validation sample labeled; agreement reported; schema locked. The benchmark must be
locked before threshold tuning (Phase 8). Several points are open in the spec.

## Decision

1. **Top-level package `driftchain_bench/`, isolated by contract.** §17 places it next to
   `src/` and calls it separately releasable. Two import-linter contracts enforce this:
   - the benchmark imports nothing from `constraintlens` or `pipelines`;
   - neither of those imports the benchmark, so the tool can never read labels.

   It is strict under mypy like the domain core. Its only new runtime dependency is
   PyYAML, which was already in the lock.
2. **Labels come from beliefs, not from text.** Each agent holds `Facts` (limit,
   approval, permission, org cap, scope, injected note) and writes its text from them.
   An operator rewrites the facts at the mutated hop (§13.1 "labels are known by
   construction"). An **oracle** compares the acting hop's tool call with the user's
   *original* constraints to derive `violated_constraints`. A test also re-derives every
   violation with ConstraintLens's own composition (`compose(...).evaluate`), and the two
   must agree on all 4,000 chains. A label that ConstraintLens's structural model reads
   differently fails CI.
3. **Reference check (§20 mutation-operator tests).** `reference.features` reads only
   the surface text: the bounds in the scenario's unit, an approval cue, a permission
   cue, a quoted tool note, and the allowed subject ids. The generator raises if any text
   it writes lacks exactly the features its facts imply. Tests check each operator's
   documented effect from the records alone (for example, NUMERIC_LOOSEN: the mutated
   hop states `limit × variant` and no longer the limit). Controls must keep the
   features of T0 at every hop.
4. **Operators** are registered plugins, one module each (§5 open/closed). Choices the
   spec leaves open:
   - NUMERIC_LOOSEN only *raises* the limit (×2/5/10); removing it is SUMMARIZATION_LOSS.
   - NEGATION_FLIP turns the bound into a permission and drops the approval clause it
     subsumes.
   - SUMMARIZATION_LOSS writes a summary frame that keeps the subject and omits every
     bound.
   - CONSTRAINT_DROP_ONE drops the org cap and keeps the looser user limit.
   - INJECTED_INSTRUCTION appends a quoted tool note; 30% are "adversarial" notes that
     claim compliance (§21.1).
   - STALE_CONTEXT leaves the text alone and makes the mutated hop's dwell exceed the
     6-hour `max_age_seconds`.
5. **Async profiles (§13.4).**
   - **Topologies:** linear (3–4 hops), fan-out (a side branch that reports but does not
     act) and fan-in (two branches join at the acting agent).
   - **Fan-in join:** last-writer-wins, so whether fan-in drift reaches the action
     depends on arrival order. That makes reordering label-relevant rather than
     cosmetic.
   - **Reordering:** swaps the sibling pair, on both the consumption and the emission
     clocks.
   - **Redelivery:** a hop's delivery count becomes 2. It is recorded but not labeled,
     because duplicates are an async slice and not a constraint violation.
   - **Freshness:** every non-stale chain stays inside the 6-hour bound, which the
     generator asserts.
6. **Record format = §13.5 plus the fields an evaluator needs**:
   - `instruction` (T0);
   - `constraints[].spec`, field for field `ConstraintSpec`, with canonical text equal to
     the domain's (tested);
   - `tools` (`ActionBinding` per tool);
   - per-hop `parents`, `consumed_at_s`, `arrival` and `surface`;
   - `label.hard_negative` and `label.variant`.

   Amounts are integers, so no Decimal-in-JSON ambiguity arises.
7. **Schema locked.** `record.ChainRec` is the single source; `schema.json` is generated
   from it and committed. Tests and `python -m driftchain_bench check` (a new CI job,
   §19.3 "benchmark schema validation") fail if they differ. A format change needs a
   `SCHEMA_VERSION` bump.
8. **Release lock.** Generation is a pure function of `(version, seed, size)`. Every draw
   has its own string-seeded `random.Random`, so a new draw never shifts others. The
   4,000-chain JSONL (10.6 MB) is gitignored (§17 "gitignored if large"). The committed
   manifest pins its SHA-256 and statistics, and CI regenerates the data and compares.
   `generate` refuses a `--version` other than its own.
9. **Splits.** Exact 20/20/60 over scenario *instances*, per scenario, so no instance or
   paraphrase seed spans two splits (§13.6 item 4). Each instance yields 4 controls (half
   hard negatives) and 4 drifted chains with distinct operators, balanced per scenario
   (§13.6 item 3).
10. **Paraphrase batch, optional and label-safe.** An LLM rewrite of a hop text is
    accepted only if its reference features equal the template's. Outputs are cached
    (`sha256(model | prompt version | text)`) and committed with a release, so
    evaluation never regenerates text (§19.4). A `--max-calls` guard bounds spend. The
    default model is `claude-sonnet-5-5`, not the judge's default `claude-opus-5-5`
    (§14.5), and the adapter uses the official SDK with a cached system prompt and low
    effort. T0 is never paraphrased: it is the user's own words.
11. **Hand validation tooling.**
    - `validate --sample 300` draws a stratified blind sample: 150 drifted chains
      round-robin over all 48 scenario × operator cells, 150 controls over all six
      scenarios.
    - Its output is a Markdown sheet, a JSONL view and a CSV template. `key.json` maps
      sample ids to chain ids and holds no labels.
    - `agreement` reports raw agreement and Cohen's kappa for drift and violation,
      culprit-hop accuracy, a per-operator breakdown, every disagreement, and
      inter-annotator kappa when a second file is given. A test regenerates the sample
      and requires it to be byte-identical to the committed one.
    - Exclusions after review go in `exclusions.json`, with reasons.
12. **CLI.** `python -m driftchain_bench generate | check | validate | agreement |
    paraphrase | schema`. Flags match §16 (`--version --seed --size`, `--sample`), so
    the Phase 9 `constraintlens bench …` command can wrap it. The `benchmark_chains`
    table of §12.2 is loaded by the evaluation harness in Phase 8, which is its only
    reader.

## Consequences

- **Exit criteria met:**
  - **3,000+ chains:** 4,000 (seed 7).
  - **Schema locked:** `schema.json`, a test, and a CI job.
- **Exit criteria not met:** "validation sample labeled; agreement reported". The blind
  sample, the guide and the agreement tooling are in place, but labeling needs people,
  and no human labels exist yet. Labels from the developer or from an LLM would be
  neither manual nor blind, so none were recorded. The dataset card says plainly that
  the labels are not validated and that no agreement figure exists. Phase 8 threshold
  tuning should not start until agreement is reported (§23 "must be locked before
  threshold tuning"; scope valves: the validation sample is never cut).
- **The LLM paraphrase pass has not run.** No API key on the development host. v0.1.0 is
  template text (`surface: template`, `llm_paraphrase_model: null`), and the card says
  so. Running it later produces a paraphrased release with its own manifest. Labels
  cannot change.
- **STALE_CONTEXT scoring is open.** The stale chains carry unchanged text, so a
  successful semantic re-verification sees entailment. Phase 8 decides how
  re-verification counts toward detection, and records it with the evaluation protocol.
- Phase 6 adds 50 tests. Generating the release takes about 2 s, so the full-dataset
  checks run in the normal test suite and in CI.
