"""NONE (control): no mutation; every hop paraphrases while preserving meaning."""

from __future__ import annotations

from typing import TYPE_CHECKING

from driftchain_bench.operators.base import Mutation, Operator, register

if TYPE_CHECKING:
    from random import Random

    from driftchain_bench.facts import Facts, Instance
    from driftchain_bench.scenarios import Scenario


@register
class NoMutation(Operator):
    name = "NONE"
    effect = "No mutation. Every hop rewords the task and keeps every constraint."
    example = "'up to $100' -> 'Anything past $100 is off the table.' (hard negative)"
    textual = False

    def apply(self, facts: Facts, inst: Instance, scenario: Scenario, rng: Random) -> Mutation:
        return Mutation(facts)
