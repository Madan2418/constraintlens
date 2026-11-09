"""NEGATION_FLIP: the prohibition becomes a permission."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from driftchain_bench.operators.base import Mutation, Operator, register

if TYPE_CHECKING:
    from random import Random

    from driftchain_bench.facts import Facts, Instance
    from driftchain_bench.scenarios import Scenario


@register
class NegationFlip(Operator):
    name = "NEGATION_FLIP"
    effect = (
        "'Do not exceed X' becomes 'may exceed X'. The amount is still stated, which is "
        "what defeats keyword and embedding checks. The approval clause goes with it."
    )
    example = "'no more than $100' -> 'it may exceed $100 if needed'"

    def apply(self, facts: Facts, inst: Instance, scenario: Scenario, rng: Random) -> Mutation:
        if facts.limit is None:
            raise ValueError("NEGATION_FLIP needs a limit")
        return Mutation(replace(facts, may_exceed=True))
