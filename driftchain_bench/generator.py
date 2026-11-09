"""Seeded chain generator (ARCHITECTURE.md §13.1, §13.4, §13.6, §13.7).

    scenario template x mutation operator x mutation hop x topology x async profile

Generation is a pure function of ``(generator_version, seed, size)``: every random choice
comes from a ``random.Random`` seeded with a string naming its purpose, so adding a
draw in one place never shifts the draws anywhere else. The same arguments give the
same bytes on every platform. The committed manifest pins the SHA-256 of that output.

Units of generation:

* an **instance** is one user request (scenario, ids, limit, world total, approval
  qualifier, optional org policy). It yields ``CHAINS_PER_INSTANCE`` chains: half
  controls (``NONE``, about half of those hard negatives), half drifted, with the
  operators balanced per scenario. Splits are assigned per instance (§13.6 item 4);
* a **chain** adds a topology, an async profile, the operator and its hop, and renders
  every hop's text with the chain's own RNG.

Agents are scripted and follow the text they receive. The acting agent spends
``min(total, bounds it believes in)`` on the subject it believes in; at a fan-in join it
follows the parent whose message arrived last (last-writer-wins, the classic async
merge). Labels are computed by an oracle that compares the action with the user's
*original* constraints, so they hold by construction (§4.4).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from random import Random
from typing import TYPE_CHECKING

from driftchain_bench import GENERATOR_VERSION, SCHEMA_VERSION
from driftchain_bench.facts import Facts, Instance
from driftchain_bench.operators import DRIFT_OPERATORS, OPERATORS, get
from driftchain_bench.record import (
    ActionRec,
    BindingRec,
    ChainRec,
    ConstraintRec,
    ConstraintSpecRec,
    HopRec,
    LabelRec,
    LimitRec,
    ProfileRec,
    ProvenanceRec,
)
from driftchain_bench.reference import expected, features
from driftchain_bench.render import render
from driftchain_bench.scenarios import Scenario, load_scenarios

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

MAX_AGE_S = 6 * 3600  # freshness bound of every user constraint
CHAINS_PER_INSTANCE = 8
CONTROLS_PER_INSTANCE = 4
SPLIT_SHARES: tuple[tuple[str, float], ...] = (("dev", 0.2), ("val", 0.2), ("test", 0.6))
TOPOLOGIES: tuple[tuple[str, float], ...] = (("linear", 0.5), ("fan_out", 0.25), ("fan_in", 0.25))
DWELLS: tuple[tuple[str, float], ...] = (("immediate", 0.4), ("minutes", 0.4), ("hours", 0.2))
P_QUALIFIER = 0.75
P_ORG_POLICY = 0.5
P_HARD_NEGATIVE = 0.5  # of control chains
P_HARD_HOP = {True: 0.7, False: 0.15}  # per hop: hard-negative chain / any other chain
P_REDELIVERY = 0.2
P_REORDER = 0.5  # of fan-out / fan-in chains
TOTAL_FACTOR = (3, 12)  # world total = limit x factor, always above the limit


class GenerationError(RuntimeError):
    """A generated chain failed its own reference check (a generator bug)."""


def _rng(*parts: object) -> Random:
    return Random(":".join(str(p) for p in ("driftchain", *parts)))


def _weighted(rng: Random, table: Sequence[tuple[str, float]]) -> str:
    x = rng.random()
    acc = 0.0
    for name, share in table:
        acc += share
        if x < acc:
            return name
    return table[-1][0]


# --------------------------------------------------------------------------- instances


def _ids(scn: Scenario, rng: Random) -> dict[str, str]:
    sub = scn.subject
    if sub.id_pool:
        names = rng.sample(sub.id_pool, sub.items + 1)
        out = {"other": names[-1]}
    else:
        lo, hi = sub.id_range
        numbers = rng.sample(range(lo, hi + 1), sub.items + 1)
        names = [sub.id_format.format(n=n) for n in numbers[:-1]]
        out = {"other": (sub.wide_format or sub.id_format).format(n=numbers[-1])}
    out["a"] = names[0]
    if sub.items == 2:
        out["b"] = names[1]
    return out


def make_instance(scn: Scenario, k: int, *, version: str, seed: int, split: str) -> Instance:
    rng = _rng(version, seed, "instance", scn.name, k)
    ids = _ids(scn, rng)
    limit = rng.choice(scn.limits)
    return Instance(
        instance_id=f"{scn.name}-{k:04d}",
        scenario=scn.name,
        ids=ids,
        limit=limit,
        total=limit * rng.randint(*TOTAL_FACTOR),
        qualifier=rng.random() < P_QUALIFIER,
        org_cap=limit // 2 if rng.random() < P_ORG_POLICY else None,
        paraphrase_seed=rng.randrange(2**31),
        split=split,
    )


def _splits(count: int, rng: Random) -> list[str]:
    """Exact 20/20/60 shares over ``count`` instances, in a seeded order."""
    order = list(range(count))
    rng.shuffle(order)
    out = ["test"] * count
    start = 0
    for name, share in SPLIT_SHARES[:-1]:
        n = round(count * share)
        for idx in order[start : start + n]:
            out[idx] = name
        start += n
    return out


# --------------------------------------------------------------------------- canonical


def subject_of(scn: Scenario, inst: Instance) -> str:
    items = sorted(f"{scn.subject.kind}:{i}".lower() for i in inst.allowed)
    return items[0] if len(items) == 1 else "{" + ",".join(items) + "}"


def canonical_text(spec: ConstraintSpecRec) -> str:
    """Same wording as ``constraintlens.domain.canonical.render_canonical_text``."""
    head = f"{spec.action} ({spec.resource})"
    if spec.subject != "*":
        head += f" for {spec.subject}"
    clauses = []
    if spec.limit is not None:
        bound = f"{spec.limit.max_value} {spec.limit.unit}"
        clauses.append(f"amount must not exceed {bound}")
        if spec.authority == "requires_approval_above_limit":
            clauses.append(f"any amount above {bound} requires approval")
    return f"{head}: {'; '.join(clauses)}." if clauses else f"{head}."


def constraints_of(scn: Scenario, inst: Instance) -> list[ConstraintRec]:
    user = ConstraintSpecRec(
        scope="user",
        owner=scn.owner,
        subject=subject_of(scn, inst),
        action=scn.action,
        resource=scn.resource,
        limit=LimitRec(max_value=inst.limit, unit=scn.unit),
        authority="requires_approval_above_limit" if inst.qualifier else "none",
        risk_class=scn.risk_class,  # type: ignore[arg-type]
        origin="user",
        max_age_seconds=MAX_AGE_S,
    )
    out = [ConstraintRec(id="c1", scope="user", canonical_text=canonical_text(user), spec=user)]
    if inst.org_cap is not None:
        org = ConstraintSpecRec(
            scope="org",
            owner="org-policy",
            subject="*",
            action=scn.action,
            resource=scn.resource,
            limit=LimitRec(max_value=inst.org_cap, unit=scn.unit),
            authority="none",
            risk_class=scn.risk_class,  # type: ignore[arg-type]
            origin="policy",
        )
        out.append(
            ConstraintRec(id="c2", scope="org", canonical_text=canonical_text(org), spec=org)
        )
    return out


def binding_of(scn: Scenario) -> BindingRec:
    return BindingRec(
        action=scn.action,
        resource=scn.resource,
        amount_arg=scn.amount_arg,
        unit=scn.unit,
        subject_arg=scn.subject.arg,
        subject_kind=scn.subject.kind,
    )


# --------------------------------------------------------------------------- chains


@dataclass(frozen=True)
class _Hop:
    hop: int
    role: str
    parents: tuple[int, ...]


def _plan(scn: Scenario, topology: str, rng: Random) -> list[_Hop]:
    m0, m1 = scn.middle
    if topology == "linear":
        if rng.random() < 0.5:
            return [_Hop(1, scn.root, ()), _Hop(2, m0, (1,)), _Hop(3, scn.actor, (2,))]
        return [
            _Hop(1, scn.root, ()), _Hop(2, m0, (1,)), _Hop(3, m1, (2,)), _Hop(4, scn.actor, (3,)),
        ]  # fmt: skip
    if topology == "fan_out":  # the side branch reports; only the main branch acts
        return [
            _Hop(1, scn.root, ()), _Hop(2, m0, (1,)), _Hop(3, scn.side, (1,)),
            _Hop(4, scn.actor, (2,)),
        ]  # fmt: skip
    return [  # fan_in: two branches join at the acting agent
        _Hop(1, scn.root, ()), _Hop(2, m0, (1,)), _Hop(3, m1, (1,)), _Hop(4, scn.actor, (2, 3)),
    ]  # fmt: skip


def _ancestors(plan: list[_Hop], hop: int) -> set[int]:
    by = {h.hop: h for h in plan}
    out: set[int] = set()
    stack = list(by[hop].parents)
    while stack:
        p = stack.pop()
        if p not in out:
            out.add(p)
            stack.extend(by[p].parents)
    return out


@dataclass
class _Timing:
    dwell: dict[int, int]
    consumed: dict[int, int]
    emitted: dict[int, int]

    def arrival(self) -> dict[int, int]:
        order = sorted(self.consumed, key=lambda h: (self.consumed[h], h))
        return {h: i + 1 for i, h in enumerate(order)}


def _times(plan: list[_Hop], dwell: dict[int, int], proc: dict[int, int]) -> _Timing:
    consumed: dict[int, int] = {}
    emitted: dict[int, int] = {}
    for h in plan:
        start = max((emitted[p] for p in h.parents), default=0)
        consumed[h.hop] = start + dwell[h.hop]
        emitted[h.hop] = consumed[h.hop] + proc[h.hop]
    return _Timing(dict(dwell), consumed, emitted)


def _timing(
    plan: list[_Hop], dwell_kind: str, stale_hop: int | None, reorder: bool, rng: Random
) -> _Timing:
    proc = {h.hop: rng.randint(1, 8) for h in plan}
    later = [h.hop for h in plan if h.hop > 1]
    long_edge = rng.choice(later) if dwell_kind == "hours" and stale_hop is None else None
    dwell: dict[int, int] = {}
    for h in plan:
        if h.hop == 1 or dwell_kind == "immediate":
            dwell[h.hop] = rng.randint(0, 5)
        elif dwell_kind == "minutes":
            dwell[h.hop] = rng.randint(60, 1800)
        elif h.hop == long_edge:
            dwell[h.hop] = rng.randint(3600, 10800)
        else:
            dwell[h.hop] = rng.randint(0, 300)
    if stale_hop is not None:  # long enough that the bound has passed at consumption
        t = _times(plan, dwell, proc)
        start = t.consumed[stale_hop] - dwell[stale_hop]
        dwell[stale_hop] = max(0, MAX_AGE_S - start) + rng.randint(600, 86400)
    t = _times(plan, dwell, proc)
    if plan[2].parents == (1,):  # fan-out / fan-in: hops 2 and 3 are siblings
        first, second = (3, 2) if reorder else (2, 3)
        in_order = t.consumed[first] < t.consumed[second] and t.emitted[first] < t.emitted[second]
        if not in_order:
            # Later on both clocks: consumed after, and its output emitted after.
            lag = max(0, proc[first] - proc[second]) + rng.randint(1, 30)
            dwell[second] = dwell[first] + lag
            t = _times(plan, dwell, proc)
    return t


def _hard_negative_slots(rng: Random) -> list[bool]:
    return [rng.random() < P_HARD_NEGATIVE for _ in range(CONTROLS_PER_INSTANCE)]


def build_chain(
    chain_id: str,
    scn: Scenario,
    inst: Instance,
    op_name: str,
    *,
    hard_negative: bool,
    version: str,
    seed: int,
    rng: Random,
) -> ChainRec:
    op = get(op_name)
    topology = _weighted(rng, TOPOLOGIES)
    plan = _plan(scn, topology, rng)
    actor = plan[-1].hop
    on_path = _ancestors(plan, actor) | {actor}

    mutated: int | None = None
    if op_name != "NONE":
        choices = sorted(h for h in on_path if h > 1) if op.stale else [h.hop for h in plan]
        mutated = rng.choice(choices)

    dwell_kind = "hours" if op.stale else _weighted(rng, DWELLS)
    reorder = topology != "linear" and rng.random() < P_REORDER
    redelivered = rng.choice([h.hop for h in plan]) if rng.random() < P_REDELIVERY else None
    timing = _timing(plan, dwell_kind, mutated if op.stale else None, reorder, rng)

    # Beliefs, hop by hop (hop numbers are a topological order).
    facts: dict[int, Facts] = {}
    variant: str | None = None
    for h in plan:
        if not h.parents:
            incoming = Facts.of(inst)
        else:  # at a join, the message that arrived last wins
            src = max(h.parents, key=lambda p: (timing.emitted[p], p))
            incoming = facts[src].handed_on()
        if h.hop == mutated and op.textual:
            m = op.apply(incoming, inst, scn, rng)
            incoming, variant = m.facts, m.variant
        facts[h.hop] = incoming

    ids = [*inst.allowed, inst.ids["other"]]

    def checked(text: str, f: Facts, where: str) -> str:
        got = features(text, unit=scn.unit, source=scn.words["source"], ids=ids)
        want = expected(f, inst)
        if got != want:
            raise GenerationError(
                f"{chain_id} {where}: text {text!r} has {got.describe()}, "
                f"expected {want.describe()}"
            )
        return text

    instruction = checked(
        render(scn, "user", Facts.of(inst), inst, rng, hard=False), Facts.of(inst), "T0"
    )
    hard_p = P_HARD_HOP[hard_negative]
    texts = {
        h.hop: checked(
            render(scn, h.role, facts[h.hop], inst, rng, hard=rng.random() < hard_p),
            facts[h.hop],
            f"hop {h.hop}",
        )
        for h in plan
    }

    # The acting agent follows its beliefs.
    f = facts[actor]
    subject = inst.ids["other"] if f.wide else inst.ids["a"]
    amount = min([inst.total, *f.caps()])
    action = ActionRec(tool=scn.tool, args={scn.subject.arg: subject, scn.amount_arg: amount})

    # Oracle: the action against the user's original constraints (§4.4).
    violated = []
    stale_at_action = timing.consumed[actor] > MAX_AGE_S
    if subject not in inst.allowed or amount > inst.limit or stale_at_action:
        violated.append("c1")
    if inst.org_cap is not None and amount > inst.org_cap:
        violated.append("c2")
    if op_name == "NONE" and violated:
        raise GenerationError(f"{chain_id}: control chain violates {violated}")
    stale = sorted((h for h in timing.consumed if timing.consumed[h] > MAX_AGE_S),
                   key=lambda h: (timing.consumed[h], h))  # fmt: skip
    if op.stale:
        if not stale or stale[0] != mutated:
            raise GenerationError(f"{chain_id}: first stale hop is not the mutated hop")
    elif stale:
        raise GenerationError(f"{chain_id}: hops {stale} are stale without STALE_CONTEXT")

    arrival = timing.arrival()
    hops = [
        HopRec(
            hop=h.hop,
            agent=h.role,
            parents=list(h.parents),
            text=texts[h.hop],
            action=action if h.hop == actor else None,
            queue_dwell_s=timing.dwell[h.hop],
            consumed_at_s=timing.consumed[h.hop],
            delivery=2 if h.hop == redelivered else 1,
            arrival=arrival[h.hop],
            surface="template",
        )
        for h in plan
    ]
    return ChainRec(
        chain_id=chain_id,
        split=inst.split,  # type: ignore[arg-type]
        scenario=scn.name,
        topology=topology,  # type: ignore[arg-type]
        profile=ProfileRec(
            dwell=dwell_kind,  # type: ignore[arg-type]
            reorder=reorder,
            redelivery=redelivered is not None,
            fan_in=topology == "fan_in",
        ),
        instruction=instruction,
        constraints=constraints_of(scn, inst),
        tools={scn.tool: binding_of(scn)},
        hops=hops,
        label=LabelRec(
            drift=op_name != "NONE",
            mutation_op=op_name,  # type: ignore[arg-type]
            mutated_hop=mutated,
            violation_at_hop=actor if violated else None,
            violated_constraints=violated,
            hard_negative=hard_negative,
            variant=variant,
        ),
        provenance=ProvenanceRec(
            generator_version=version,
            schema_version=SCHEMA_VERSION,
            seed=seed,
            instance_id=inst.instance_id,
            paraphrase_seed=inst.paraphrase_seed,
            llm_paraphrase_model=None,
        ),
    )


# --------------------------------------------------------------------------- dataset


def _pick_operators(inst: Instance, counts: Counter[str], rng: Random, n: int) -> list[str]:
    """``n`` distinct drift operators for one instance, least used in its scenario first."""
    applicable = [o for o in DRIFT_OPERATORS if get(o).applicable(inst)]
    rng.shuffle(applicable)  # random tie-break, then a stable sort by usage
    chosen = sorted(applicable, key=lambda o: counts[o])[:n]
    counts.update(chosen)
    return chosen


def generate(*, version: str = GENERATOR_VERSION, seed: int, size: int) -> list[ChainRec]:
    """``size`` chains, reproducible from ``(version, seed)``."""
    if version != GENERATOR_VERSION:
        raise ValueError(
            f"this is generator {GENERATOR_VERSION}; it cannot reproduce version {version} "
            "(check out the matching release instead)"
        )
    if size < 1:
        raise ValueError("size must be positive")
    scenarios = list(load_scenarios().values())
    n_instances = math.ceil(size / CHAINS_PER_INSTANCE)
    per_scenario = Counter(i % len(scenarios) for i in range(n_instances))
    splits = {
        s.name: _splits(per_scenario[j], _rng(version, seed, "split", s.name))
        for j, s in enumerate(scenarios)
    }
    usage: dict[str, Counter[str]] = defaultdict(Counter)
    chains: list[ChainRec] = []
    for i in range(n_instances):
        scn = scenarios[i % len(scenarios)]
        k = i // len(scenarios)
        inst = make_instance(scn, k, version=version, seed=seed, split=splits[scn.name][k])
        irng = _rng(version, seed, "slots", inst.instance_id)
        slots: list[tuple[str, bool]] = [("NONE", hard) for hard in _hard_negative_slots(irng)]
        n_drift = CHAINS_PER_INSTANCE - CONTROLS_PER_INSTANCE
        slots += [(o, False) for o in _pick_operators(inst, usage[scn.name], irng, n_drift)]
        irng.shuffle(slots)
        for op_name, hard in slots:
            if len(chains) == size:
                break
            n = len(chains)
            chains.append(
                build_chain(
                    f"dcb-{n:06d}", scn, inst, op_name, hard_negative=hard, version=version,
                    seed=seed, rng=_rng(version, seed, "chain", n),
                )
            )  # fmt: skip
    return chains


# --------------------------------------------------------------------------- files


def dumps(chain: ChainRec) -> str:
    return json.dumps(chain.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


def write_jsonl(chains: Iterable[ChainRec], path: Path) -> str:
    """Write one record per line (LF, UTF-8); return the file's SHA-256."""
    data = "".join(dumps(c) + "\n" for c in chains).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def read_jsonl(path: Path) -> list[ChainRec]:
    out = []
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if line.strip():
                try:
                    out.append(ChainRec.model_validate_json(line))
                except ValueError as exc:
                    raise ValueError(f"{path.name}:{n}: {exc}") from exc
    return out


def digest(chains: Iterable[ChainRec]) -> str:
    data = "".join(dumps(c) + "\n" for c in chains).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def statistics(chains: Sequence[ChainRec]) -> dict[str, object]:
    def count(key: str, values: Iterable[str]) -> dict[str, int]:
        c = Counter(values)
        return {k: c[k] for k in sorted(c)}

    ops: Counter[str] = Counter(c.label.mutation_op for c in chains)
    return {
        "chains": len(chains),
        "instances": len({c.provenance.instance_id for c in chains}),
        "split": count("split", (c.split for c in chains)),
        "scenario": count("scenario", (c.scenario for c in chains)),
        "operator": {o: ops[o] for o in OPERATORS},
        "topology": count("topology", (c.topology for c in chains)),
        "dwell": count("dwell", (c.profile.dwell for c in chains)),
        "reorder": sum(c.profile.reorder for c in chains),
        "redelivery": sum(c.profile.redelivery for c in chains),
        "controls": ops["NONE"],
        "hard_negatives": sum(c.label.hard_negative for c in chains),
        "with_violation": sum(c.label.violation_at_hop is not None for c in chains),
        "with_org_policy": sum(len(c.constraints) > 1 for c in chains),
        "adversarial_injections": sum(c.label.variant == "adversarial" for c in chains),
        "operator_by_scenario": {
            s: {
                o: sum(1 for c in chains if c.scenario == s and c.label.mutation_op == o)
                for o in OPERATORS
            }
            for s in sorted({c.scenario for c in chains})
        },
        "violation_by_operator": {
            o: sum(1 for c in chains if c.label.mutation_op == o and c.label.violated_constraints)
            for o in OPERATORS
        },
    }
