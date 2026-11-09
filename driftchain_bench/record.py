"""The driftchain-bench record format (ARCHITECTURE.md §13.5), as a Pydantic model.

This model is the schema's single source of truth. ``schema.json`` is generated from it
and committed, and a test fails if the two drift apart, which is what "schema locked"
means (§23 Phase 6). Validators encode the invariants a consumer may rely on: hops are
numbered in topological order, labels point at real hops, a control has no drift.

Additions to the §13.5 example, all needed by an evaluator:

* ``instruction``: T_0, the user's original words (hop 1's text is T_1).
* ``constraints[].spec``: the structured constraint, field for field the
  ``ConstraintSpec`` of ConstraintLens, so it can be registered without re-extraction.
* ``tools``: how each tool's arguments map onto constraint dimensions (Tier 1a).
* ``hops[].parents`` / ``arrival`` / ``consumed_at_s`` / ``surface``: the DAG edges,
  global consumption order, consumption time since issue, and whether the text is a
  template rendering or an accepted LLM paraphrase.
* ``profile``: the async profile of §13.4.
* ``label.hard_negative`` / ``label.variant``: control difficulty and operator variant.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Split = Literal["dev", "val", "test"]
Topology = Literal["linear", "fan_out", "fan_in"]
Dwell = Literal["immediate", "minutes", "hours"]
OperatorName = Literal[
    "NUMERIC_LOOSEN",
    "QUALIFIER_DELETE",
    "NEGATION_FLIP",
    "SCOPE_WIDEN",
    "SUMMARIZATION_LOSS",
    "STALE_CONTEXT",
    "CONSTRAINT_DROP_ONE",
    "INJECTED_INSTRUCTION",
    "NONE",
]
SCHEMA_ID = "https://github.com/Madan2418/constraintlens/driftchain_bench/schema.json"


class _Rec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LimitRec(_Rec):
    max_value: int = Field(gt=0)
    unit: str = Field(min_length=1)


class ConstraintSpecRec(_Rec):
    """Mirrors ``constraintlens.domain.models.ConstraintSpec`` (a test checks it)."""

    scope: Literal["user", "org"]
    owner: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    action: str = Field(min_length=1)
    resource: str = Field(min_length=1)
    limit: LimitRec | None
    authority: Literal["requires_approval_above_limit", "none"]
    risk_class: Literal["high", "low"]
    origin: Literal["user", "policy"]
    max_age_seconds: int | None = Field(default=None, gt=0)


class ConstraintRec(_Rec):
    id: str = Field(pattern=r"^c[0-9]+$")
    scope: Literal["user", "org"]
    canonical_text: str = Field(min_length=1)
    spec: ConstraintSpecRec


class BindingRec(_Rec):
    """Mirrors ``constraintlens.domain.models.ActionBinding``."""

    action: str
    resource: str
    amount_arg: str
    unit: str
    subject_arg: str
    subject_kind: str


class ActionRec(_Rec):
    tool: str = Field(min_length=1)
    args: dict[str, int | str]


class HopRec(_Rec):
    hop: int = Field(ge=1)
    agent: str = Field(min_length=1)
    parents: list[int]
    text: str = Field(min_length=1)
    action: ActionRec | None
    queue_dwell_s: int = Field(ge=0)
    consumed_at_s: int = Field(ge=0)  # seconds since the constraints were issued
    delivery: int = Field(ge=1)
    arrival: int = Field(ge=1)  # position in the global consumption order
    surface: str = Field(pattern=r"^(template|llm:.+)$")


class ProfileRec(_Rec):
    dwell: Dwell
    reorder: bool  # two sibling hops consumed in the opposite order to their hop numbers
    redelivery: bool  # one hop delivered twice (at-least-once transport)
    fan_in: bool


class LabelRec(_Rec):
    drift: bool
    mutation_op: OperatorName
    mutated_hop: int | None
    violation_at_hop: int | None
    violated_constraints: list[str]
    hard_negative: bool
    variant: str | None

    @model_validator(mode="after")
    def _consistent(self) -> LabelRec:
        control = self.mutation_op == "NONE"
        if self.drift == control:
            raise ValueError("drift must be true exactly when mutation_op is not NONE")
        if (self.mutated_hop is None) != control:
            raise ValueError("mutated_hop is set exactly for drifted chains")
        if (self.violation_at_hop is None) != (not self.violated_constraints):
            raise ValueError("violation_at_hop and violated_constraints go together")
        if control and self.violated_constraints:
            raise ValueError("a control chain cannot violate a constraint")
        if self.hard_negative and not control:
            raise ValueError("hard_negative marks control chains only")
        return self


class ProvenanceRec(_Rec):
    generator_version: str
    schema_version: str
    seed: int
    instance_id: str
    paraphrase_seed: int
    llm_paraphrase_model: str | None


class ChainRec(_Rec):
    chain_id: str = Field(pattern=r"^dcb-[0-9]{6}$")
    split: Split
    scenario: str
    topology: Topology
    profile: ProfileRec
    instruction: str = Field(min_length=1)
    constraints: list[ConstraintRec] = Field(min_length=1)
    tools: dict[str, BindingRec]
    hops: list[HopRec] = Field(min_length=2)
    label: LabelRec
    provenance: ProvenanceRec

    @model_validator(mode="after")
    def _consistent(self) -> ChainRec:
        numbers = [h.hop for h in self.hops]
        if numbers != list(range(1, len(self.hops) + 1)):
            raise ValueError("hops must be numbered 1..n in order")
        if self.hops[0].parents:
            raise ValueError("hop 1 is the root and has no parents")
        for h in self.hops[1:]:
            if not h.parents or any(p >= h.hop or p < 1 for p in h.parents):
                raise ValueError(f"hop {h.hop}: parents must be earlier hops")
            if len(set(h.parents)) != len(h.parents):
                raise ValueError(f"hop {h.hop}: duplicate parent")
        if sorted(h.arrival for h in self.hops) != numbers:
            raise ValueError("arrival must be a permutation of 1..n")
        if self.profile.fan_in != (self.topology == "fan_in"):
            raise ValueError("profile.fan_in must match the topology")
        for h in self.hops:
            if h.action is not None and h.action.tool not in self.tools:
                raise ValueError(f"hop {h.hop}: tool {h.action.tool!r} has no binding")
        ids = {c.id for c in self.constraints}
        if len(ids) != len(self.constraints):
            raise ValueError("duplicate constraint id")
        lab = self.label
        for hop in (lab.mutated_hop, lab.violation_at_hop):
            if hop is not None and not 1 <= hop <= len(self.hops):
                raise ValueError(f"label names hop {hop}, which does not exist")
        if lab.violation_at_hop is not None and self.hops[lab.violation_at_hop - 1].action is None:
            raise ValueError("violation_at_hop must be a hop with an action")
        if not set(lab.violated_constraints) <= ids:
            raise ValueError("violated_constraints names an unknown constraint")
        return self
