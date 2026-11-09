"""STALE_CONTEXT: the constraint was valid at issue but is outdated at consumption."""

from __future__ import annotations

from typing import TYPE_CHECKING

from driftchain_bench.operators.base import Mutation, Operator, register

if TYPE_CHECKING:
    from random import Random

    from driftchain_bench.facts import Facts, Instance
    from driftchain_bench.scenarios import Scenario


@register
class StaleContext(Operator):
    name = "STALE_CONTEXT"
    effect = (
        "The text is untouched. The mutated hop sits in the queue long enough that the "
        "user constraint's freshness bound (max_age_seconds) has passed when it is "
        "consumed, and the task is acted on without re-verification."
    )
    example = "a 9-hour queue dwell before the payments agent, bound 6 hours"
    textual = False
    stale = True

    def apply(self, facts: Facts, inst: Instance, scenario: Scenario, rng: Random) -> Mutation:
        return Mutation(facts)
