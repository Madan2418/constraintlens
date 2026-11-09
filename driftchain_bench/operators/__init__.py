"""Mutation operators (ARCHITECTURE.md §13.3), one module per operator.

Importing this package registers every operator. ``OPERATORS`` lists them in the order
of the spec table, which is also the order statistics are reported in.
"""

from __future__ import annotations

from driftchain_bench.operators import (  # noqa: F401  (imported to register)
    constraint_drop_one,
    injected_instruction,
    negation_flip,
    none,
    numeric_loosen,
    qualifier_delete,
    scope_widen,
    stale_context,
    summarization_loss,
)
from driftchain_bench.operators.base import REGISTRY, Mutation, Operator

OPERATORS: tuple[str, ...] = (
    "NUMERIC_LOOSEN",
    "QUALIFIER_DELETE",
    "NEGATION_FLIP",
    "SCOPE_WIDEN",
    "SUMMARIZATION_LOSS",
    "STALE_CONTEXT",
    "CONSTRAINT_DROP_ONE",
    "INJECTED_INSTRUCTION",
    "NONE",
)
DRIFT_OPERATORS: tuple[str, ...] = OPERATORS[:-1]

if set(OPERATORS) != set(REGISTRY):  # pragma: no cover - guards a missing registration
    raise RuntimeError(f"operator registry mismatch: {sorted(set(OPERATORS) ^ set(REGISTRY))}")


def get(name: str) -> Operator:
    return REGISTRY[name]


__all__ = ["DRIFT_OPERATORS", "OPERATORS", "REGISTRY", "Mutation", "Operator", "get"]
