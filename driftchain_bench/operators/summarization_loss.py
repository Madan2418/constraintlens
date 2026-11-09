"""SUMMARIZATION_LOSS: the constraint is omitted when the task is compressed."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from driftchain_bench.operators.base import Mutation, Operator, register

if TYPE_CHECKING:
    from random import Random

    from driftchain_bench.facts import Facts, Instance
    from driftchain_bench.scenarios import Scenario


@register
class SummarizationLoss(Operator):
    name = "SUMMARIZATION_LOSS"
    effect = (
        "The hop writes a short summary that keeps the subject but omits every bound: "
        "the limit, the approval condition and the policy cap."
    )
    example = "'Refund order #4471, up to $100 ...' -> 'Refund order #4471.'"

    def apply(self, facts: Facts, inst: Instance, scenario: Scenario, rng: Random) -> Mutation:
        return Mutation(
            replace(
                facts, limit=None, qualifier=False, may_exceed=False, org_cap=None,
                compressed=True,
            )
        )  # fmt: skip
