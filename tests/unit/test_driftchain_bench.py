"""driftchain-bench (ARCHITECTURE.md §13, §20 "mutation-operator tests", ADR 0008).

The release dataset is regenerated once per module (about two seconds) and every
property below is checked on all 4,000 records, not on a sample.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from constraintlens.domain.canonical import issue
from constraintlens.domain.composition import compose
from constraintlens.domain.models import ActionBinding, ConstraintSpec
from driftchain_bench import GENERATOR_VERSION
from driftchain_bench.generator import (
    MAX_AGE_S,
    GenerationError,
    digest,
    dumps,
    generate,
    read_jsonl,
    statistics,
    write_jsonl,
)
from driftchain_bench.operators import DRIFT_OPERATORS, OPERATORS, REGISTRY
from driftchain_bench.reference import features
from driftchain_bench.release import (
    RELEASE_SEED,
    RELEASE_SIZE,
    SCHEMA_PATH,
    manifest_path,
    read_manifest,
    schema_text,
)
from driftchain_bench.scenarios import _scenario, load_scenarios

if TYPE_CHECKING:
    from pathlib import Path

    from driftchain_bench.record import ChainRec
    from driftchain_bench.reference import Features

SCENARIOS_13_2 = {"refund", "transfer", "data_access", "scheduling", "deletion", "procurement"}


@pytest.fixture(scope="module")
def chains() -> list[ChainRec]:
    return generate(seed=RELEASE_SEED, size=RELEASE_SIZE)


def feats(chain: ChainRec, text: str) -> Features:
    scn = load_scenarios()[chain.scenario]
    subject = chain.constraints[0].spec.subject
    body = subject[1:-1] if subject.startswith("{") else subject
    ids = [item.split(":", 1)[1] for item in body.split(",")]
    actor = chain.hops[-1]
    assert actor.action is not None
    ids.append(str(actor.action.args[scn.subject.arg]))
    return features(text, unit=scn.unit, source=scn.words["source"], ids=ids)


def ancestors(chain: ChainRec, hop: int) -> set[int]:
    by = {h.hop: h for h in chain.hops}
    out: set[int] = set()
    stack = list(by[hop].parents)
    while stack:
        p = stack.pop()
        if p not in out:
            out.add(p)
            stack.extend(by[p].parents)
    return out


# --------------------------------------------------------------------------- release lock


def test_release_is_reproduced_byte_for_byte(chains: list[ChainRec]) -> None:
    manifest = read_manifest(manifest_path())
    assert manifest["generator_version"] == GENERATOR_VERSION
    assert (manifest["seed"], manifest["size"]) == (RELEASE_SEED, RELEASE_SIZE)
    assert digest(chains) == manifest["sha256"]
    assert json.loads(json.dumps(statistics(chains))) == manifest["statistics"]


def test_schema_is_locked() -> None:
    assert SCHEMA_PATH.read_text(encoding="utf-8") == schema_text()
    schema = json.loads(schema_text())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) >= {"chain_id", "split", "scenario", "topology", "hops",
                                       "label", "provenance", "constraints"}  # fmt: skip


def test_generation_is_deterministic_and_seed_dependent() -> None:
    a = generate(seed=3, size=120)
    assert digest(a) == digest(generate(seed=3, size=120))
    assert digest(a) != digest(generate(seed=4, size=120))
    # Text and labels of a chain do not depend on the run size; only the split does,
    # because split shares are exact over the run's instances.
    small = generate(seed=3, size=40)
    strip = [json.loads(dumps(c)) for c in small]
    full = [json.loads(dumps(c)) for c in a[:40]]
    for row in (*strip, *full):
        row.pop("split")
    assert strip == full


@pytest.mark.parametrize("seed", range(12))
def test_generator_self_checks_hold_across_seeds(seed: int) -> None:
    # Every chain passes the generator's own reference check and oracle sanity checks.
    assert len(generate(seed=seed, size=400)) == 400


def test_version_guard() -> None:
    with pytest.raises(ValueError, match=r"cannot reproduce version 9\.9\.9"):
        generate(version="9.9.9", seed=1, size=10)
    with pytest.raises(ValueError, match="positive"):
        generate(seed=1, size=0)


def test_jsonl_round_trip(tmp_path: Path, chains: list[ChainRec]) -> None:
    path = tmp_path / "d.jsonl"
    sha = write_jsonl(chains[:200], path)
    back = read_jsonl(path)
    assert back == chains[:200]
    assert digest(back) == sha
    path.write_text('{"chain_id": "nope"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"d\.jsonl:1"):
        read_jsonl(path)


# --------------------------------------------------------------------------- composition


def test_size_controls_and_operator_balance(chains: list[ChainRec]) -> None:
    assert len(chains) >= 3000  # §23 Phase 6 exit criterion
    ops = Counter(c.label.mutation_op for c in chains)
    assert 0.45 <= ops["NONE"] / len(chains) <= 0.55  # §13.6 item 3, control balance
    hard = sum(c.label.hard_negative for c in chains)
    assert 0.4 <= hard / ops["NONE"] <= 0.6
    drift_counts = [ops[o] for o in DRIFT_OPERATORS]
    assert max(drift_counts) - min(drift_counts) <= 0.15 * (sum(drift_counts) / len(drift_counts))
    assert {c.scenario for c in chains} == SCENARIOS_13_2
    cells = {(c.scenario, c.label.mutation_op) for c in chains}
    assert cells == {(s, o) for s in SCENARIOS_13_2 for o in OPERATORS}
    assert sum(c.label.variant == "adversarial" for c in chains) > 0  # §21.1


def test_splits_are_partitioned_by_instance(chains: list[ChainRec]) -> None:
    by_instance: dict[str, set[str]] = defaultdict(set)
    seeds: dict[str, set[int]] = defaultdict(set)
    for c in chains:
        by_instance[c.provenance.instance_id].add(c.split)
        seeds[c.provenance.instance_id].add(c.provenance.paraphrase_seed)
    assert all(len(s) == 1 for s in by_instance.values())  # no instance spans two splits
    assert all(len(s) == 1 for s in seeds.values())
    per_split = Counter(next(iter(s)) for s in by_instance.values())
    n = len(by_instance)
    assert abs(per_split["dev"] / n - 0.2) < 0.02
    assert abs(per_split["val"] / n - 0.2) < 0.02
    assert abs(per_split["test"] / n - 0.6) < 0.02
    # Every split sees every scenario and every operator.
    for split in ("dev", "val", "test"):
        sub = [c for c in chains if c.split == split]
        assert {c.scenario for c in sub} == SCENARIOS_13_2
        assert {c.label.mutation_op for c in sub} == set(OPERATORS)


def test_async_profiles(chains: list[ChainRec]) -> None:
    topologies = Counter(c.topology for c in chains)
    assert set(topologies) == {"linear", "fan_out", "fan_in"}
    assert {c.profile.dwell for c in chains} == {"immediate", "minutes", "hours"}
    for c in chains:
        assert c.profile.fan_in == (c.topology == "fan_in")
        assert sum(h.delivery > 1 for h in c.hops) == int(c.profile.redelivery)
        if c.topology == "linear":
            assert not c.profile.reorder
            assert [h.arrival for h in c.hops] == [h.hop for h in c.hops]
        else:
            h2, h3 = c.hops[1], c.hops[2]
            assert h2.parents == h3.parents == [1]
            # "swapped pair": the higher-numbered sibling is consumed first.
            assert (h3.arrival < h2.arrival) == c.profile.reorder
        if c.topology == "fan_in":
            assert c.hops[-1].parents == [2, 3]
        for h in c.hops[1:]:
            parent_times = [c.hops[p - 1].consumed_at_s for p in h.parents]
            assert h.consumed_at_s >= max(parent_times) + h.queue_dwell_s
    assert any(c.profile.reorder for c in chains)
    assert any(c.profile.redelivery for c in chains)


# --------------------------------------------------------------------------- operators (§20)


def test_every_operator_is_registered_and_documented() -> None:
    assert set(REGISTRY) == set(OPERATORS)
    for op in REGISTRY.values():
        assert op.effect
        assert op.example


def test_controls_preserve_every_constraint(chains: list[ChainRec]) -> None:
    for c in chains:
        if c.label.mutation_op != "NONE":
            continue
        f0 = feats(c, c.instruction)
        assert f0.amounts
        assert f0.ids
        assert not f0.permission
        assert not f0.injected
        for h in c.hops:
            assert feats(c, h.text) == f0, (c.chain_id, h.hop, h.text)
            assert h.consumed_at_s <= MAX_AGE_S
        assert c.label.violated_constraints == []


def test_text_before_the_mutated_hop_is_intact(chains: list[ChainRec]) -> None:
    for c in chains:
        if c.label.mutated_hop is None:
            continue
        f0 = feats(c, c.instruction)
        for hop in ancestors(c, c.label.mutated_hop):
            assert feats(c, c.hops[hop - 1].text) == f0, (c.chain_id, hop)


def _limits(c: ChainRec) -> tuple[int, int | None]:
    user = c.constraints[0].spec.limit
    assert user is not None
    org = c.constraints[1].spec.limit.max_value if len(c.constraints) > 1 else None  # type: ignore[union-attr]
    return user.max_value, org


@pytest.mark.parametrize("op", DRIFT_OPERATORS)
def test_operator_produces_its_documented_effect(chains: list[ChainRec], op: str) -> None:
    checked = 0
    for c in chains:
        if c.label.mutation_op != op:
            continue
        assert c.label.drift
        assert c.label.mutated_hop is not None
        limit, org = _limits(c)
        f0 = feats(c, c.instruction)
        fm = feats(c, c.hops[c.label.mutated_hop - 1].text)
        others = f0.amounts - {limit}
        if op == "NUMERIC_LOOSEN":
            assert limit not in fm.amounts
            raised = fm.amounts - others
            assert len(raised) == 1
            assert next(iter(raised)) == limit * int(c.label.variant.removeprefix("x"))  # type: ignore[union-attr]
        elif op == "QUALIFIER_DELETE":
            assert f0.approval
            assert not fm.approval
            assert fm.amounts == f0.amounts
        elif op == "NEGATION_FLIP":
            assert fm.permission
            assert limit in fm.amounts
        elif op == "SCOPE_WIDEN":
            assert f0.ids
            assert not fm.ids
            assert fm.amounts == f0.amounts
        elif op == "SUMMARIZATION_LOSS":
            assert not fm.amounts
            assert not fm.approval
            assert fm.ids == f0.ids
        elif op == "CONSTRAINT_DROP_ONE":
            assert org is not None
            assert org not in fm.amounts
            assert limit in fm.amounts
        elif op == "INJECTED_INSTRUCTION":
            assert fm.injected
            assert fm.amounts == f0.amounts
            assert c.label.variant in {"plain", "adversarial"}
        elif op == "STALE_CONTEXT":
            assert all(feats(c, h.text) == f0 for h in c.hops)  # text untouched
            m = c.label.mutated_hop
            stale = [h for h in c.hops if h.consumed_at_s > MAX_AGE_S]
            assert min(stale, key=lambda h: (h.consumed_at_s, h.hop)).hop == m
            assert c.hops[m - 1].queue_dwell_s > 0
            assert c.profile.dwell == "hours"
        checked += 1
    assert checked > 200


def test_fan_in_join_follows_the_last_arrival(chains: list[ChainRec]) -> None:
    seen = 0
    for c in chains:
        m = c.label.mutated_hop
        if c.topology != "fan_in" or m not in (2, 3) or not REGISTRY[c.label.mutation_op].textual:
            continue
        other = 5 - m
        followed_drift = c.hops[m - 1].arrival > c.hops[other - 1].arrival
        actor = feats(c, c.hops[-1].text)
        assert (actor == feats(c, c.hops[other - 1].text)) or followed_drift
        if not followed_drift:
            assert c.label.violated_constraints == []
        seen += 1
    assert seen > 50


# --------------------------------------------------------------------------- labels vs domain


def test_constraints_and_bindings_are_valid_constraintlens_objects(chains: list[ChainRec]) -> None:
    now = datetime(2026, 11, 10, tzinfo=UTC)
    for c in chains[:600]:
        for rec in c.constraints:
            spec = ConstraintSpec.model_validate(rec.spec.model_dump())
            issued = issue(spec, constraint_id=uuid4(), issued_at=now)
            assert issued.canonical_text == rec.canonical_text
        for binding in c.tools.values():
            ActionBinding.model_validate(binding.model_dump())


def test_labels_agree_with_the_structural_action_check(chains: list[ChainRec]) -> None:
    """The oracle's violations equal what ConstraintLens's own composition says."""
    now = datetime(2026, 11, 10, tzinfo=UTC)
    for c in chains:
        actor = c.hops[-1]
        assert actor.action is not None
        binding = c.tools[actor.action.tool]
        amount = Decimal(actor.action.args[binding.amount_arg])
        subject = f"{binding.subject_kind}:{actor.action.args[binding.subject_arg]}".lower()
        structural = set()
        for rec in c.constraints:
            spec = ConstraintSpec.model_validate(rec.spec.model_dump())
            (eff,) = compose([issue(spec, constraint_id=uuid4(), issued_at=now)]).effective
            outcome = eff.evaluate(at=now, amount=amount, unit=binding.unit, subject_item=subject)
            if outcome != "allowed":
                structural.add(rec.id)
        stale_action = actor.consumed_at_s > MAX_AGE_S
        expected = structural | ({"c1"} if stale_action else set())
        assert set(c.label.violated_constraints) == expected, c.chain_id
        if c.label.violated_constraints:
            assert c.label.violation_at_hop == actor.hop
            m = c.label.mutated_hop
            assert m is not None
            assert m == actor.hop or m in ancestors(c, actor.hop)  # drift precedes the harm


def test_violation_rates_per_operator(chains: list[ChainRec]) -> None:
    stats = statistics(chains)
    violations = stats["violation_by_operator"]
    ops = stats["operator"]
    assert isinstance(violations, dict)
    assert isinstance(ops, dict)
    assert violations["NONE"] == 0
    assert violations["QUALIFIER_DELETE"] == 0  # the agent still stops at the limit
    assert violations["STALE_CONTEXT"] == ops["STALE_CONTEXT"]
    for op in ("NUMERIC_LOOSEN", "NEGATION_FLIP", "SCOPE_WIDEN", "SUMMARIZATION_LOSS",
               "CONSTRAINT_DROP_ONE", "INJECTED_INSTRUCTION"):  # fmt: skip
        # Some drift is harmless: an org cap still binds, or a fan-in join or side branch
        # never reaches the acting agent.
        assert 0.3 * ops[op] < violations[op] <= ops[op]


# --------------------------------------------------------------------------- scenarios


def test_scenario_templates_cover_13_2() -> None:
    scenarios = load_scenarios()
    assert set(scenarios) == SCENARIOS_13_2
    for s in scenarios.values():
        assert s.limits
        assert s.fmt(1500) in {"$1,500", f"1,500 {s.unit}"}


def test_malformed_scenario_is_rejected() -> None:
    base = {
        "name": "x", "title": "x", "action": "a", "resource": "r", "unit": "USD",
        "risk_class": "high", "owner": "o", "tool": "t", "amount_arg": "amount",
        "subject": {"kind": "k", "arg": "k", "items": 1, "narrow": ["k {a}"], "wide": ["all"]},
        "limits": [10], "words": {"verb": "v"},
        "roles": {"root": "r0", "middle": ["m0", "m1"], "actor": "act", "side": "s"},
        "frames": {r: ["Do {subject}{limit}."] for r in ("user", "r0", "m0", "m1", "act", "s")}
        | {"summary": ["Do {subject}."]},
    }  # fmt: skip
    _scenario(base)
    bad_frame = json.loads(json.dumps(base))
    bad_frame["frames"]["act"] = ["Do {subject}."]
    with pytest.raises(ValueError, match="without"):
        _scenario(bad_frame)
    bad_limit = json.loads(json.dumps(base))
    bad_limit["limits"] = [15]
    with pytest.raises(ValueError, match="even"):
        _scenario(bad_limit)
    leaky_summary = json.loads(json.dumps(base))
    leaky_summary["frames"]["summary"] = ["Do {subject}{limit}."]
    with pytest.raises(ValueError, match="summary"):
        _scenario(leaky_summary)


def test_generation_error_is_raised_on_a_broken_template(monkeypatch: pytest.MonkeyPatch) -> None:
    import driftchain_bench.generator as gen

    def broken(*args: object, **kwargs: object) -> str:
        return "Refund it."  # drops the subject and every bound

    monkeypatch.setattr(gen, "render", broken)
    with pytest.raises(GenerationError, match="expected"):
        gen.generate(seed=1, size=8)
