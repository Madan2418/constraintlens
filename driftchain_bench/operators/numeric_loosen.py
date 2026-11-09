"""NUMERIC_LOOSEN: the numeric limit is raised.

The limit is multiplied by 2, 5 or 10 (the variant). Removing the limit outright is
covered by SUMMARIZATION_LOSS, so it is not repeated here.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from driftchain_bench.operators.base import Mutation, Operator, register

if TYPE_CHECKING:
    from random import Random

    from driftchain_bench.facts import Facts, Instance
    from driftchain_bench.scenarios import Scenario

FACTORS = (2, 5, 10)


@register
class NumericLoosen(Operator):
    name = "NUMERIC_LOOSEN"
    effect = "The user's limit is raised (x2, x5 or x10) from this hop on."
    example = "'up to $100' -> 'up to $1,000'"

    def apply(self, facts: Facts, inst: Instance, scenario: Scenario, rng: Random) -> Mutation:
        if facts.limit is None:
            raise ValueError("NUMERIC_LOOSEN needs a limit")
        factor = rng.choice(FACTORS)
        return Mutation(replace(facts, limit=facts.limit * factor), variant=f"x{factor}")
