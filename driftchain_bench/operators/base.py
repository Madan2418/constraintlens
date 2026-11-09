"""Mutation-operator plugin interface and registry (ARCHITECTURE.md §13.3).

Each operator is one module that subclasses ``Operator`` and registers itself. The
generator only talks to the registry, so adding an operator never edits existing code
(open/closed, §5). An operator rewrites an agent's beliefs; the text follows from them,
so the ground-truth effect is known by construction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from random import Random

    from driftchain_bench.facts import Facts, Instance
    from driftchain_bench.scenarios import Scenario


@dataclass(frozen=True)
class Mutation:
    facts: Facts
    variant: str | None = None  # e.g. "x10", "adversarial"


class Operator(ABC):
    name: ClassVar[str]
    effect: ClassVar[str]  # the documented ground-truth effect (dataset card)
    example: ClassVar[str]
    #: False when the operator leaves the text alone (STALE_CONTEXT acts on timing).
    textual: ClassVar[bool] = True
    #: The mutated hop is consumed after the constraint's freshness bound.
    stale: ClassVar[bool] = False

    def applicable(self, inst: Instance) -> bool:
        """Whether this operator has something to act on in this instance."""
        return True

    @abstractmethod
    def apply(self, facts: Facts, inst: Instance, scenario: Scenario, rng: Random) -> Mutation:
        """Rewrite the beliefs of the hop where drift is introduced."""


REGISTRY: dict[str, Operator] = {}


def register(cls: type[Operator]) -> type[Operator]:
    if cls.name in REGISTRY:
        raise ValueError(f"operator {cls.name} registered twice")
    REGISTRY[cls.name] = cls()
    return cls
