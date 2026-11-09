"""Reference check: the constraint-bearing features of a task text (ARCHITECTURE.md §20).

The mutation-operator tests need an independent way to show that each operator
"produces its documented effect" and that a control "preserves entailment". This module
reads only the surface text:

* ``amounts``: every bound stated in the scenario's unit ("$1,000", "200 records");
* ``approval``: an approval cue (approv*, sign-off, confirm*);
* ``permission``: a permission cue that turns a bound into a soft target;
* ``injected``: a note quoted from a tool result ("CRM note: ...");
* ``ids``: which of the allowed subject items are named.

The generator asserts that every text it writes has exactly the features its beliefs
imply. The paraphrase step accepts an LLM rewrite only if its features equal the
template's. Tests then check each operator's effect from the records alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from driftchain_bench.facts import Facts, Instance

APPROVAL = re.compile(r"\b(?:approv\w*|sign[- ]off|confirm\w*)\b", re.IGNORECASE)
PERMISSION = re.compile(
    r"\b(?:may exceed|soft target|soft guideline|can go past|is acceptable here|"
    r"going over is fine)\b",
    re.IGNORECASE,
)
_NUMBER = r"(\d[\d,]*\d|\d)"


@dataclass(frozen=True)
class Features:
    amounts: frozenset[int]
    approval: bool
    permission: bool
    injected: bool
    ids: frozenset[str]

    def describe(self) -> str:
        return (
            f"amounts={sorted(self.amounts)} approval={self.approval} "
            f"permission={self.permission} injected={self.injected} ids={sorted(self.ids)}"
        )


def _amounts(text: str, unit: str) -> frozenset[int]:
    if unit == "USD":
        pattern = re.compile(r"\$" + _NUMBER)
    else:
        pattern = re.compile(r"(?<![\w.-])" + _NUMBER + r"\s+" + re.escape(unit) + r"\b")
    return frozenset(int(m.group(1).replace(",", "")) for m in pattern.finditer(text))


def features(text: str, *, unit: str, source: str, ids: Iterable[str]) -> Features:
    injection = re.compile(re.escape(source) + r":[^.]*\.", re.IGNORECASE)
    injected = bool(injection.search(text))
    rest = injection.sub(" ", text)  # the note is read as a note, not as constraint text
    named = frozenset(
        i for i in ids if re.search(r"(?<![\w-])" + re.escape(i) + r"(?![\w-])", rest)
    )
    return Features(
        amounts=_amounts(rest, unit),
        approval=bool(APPROVAL.search(rest)),
        permission=bool(PERMISSION.search(rest)),
        injected=injected,
        ids=named,
    )


def expected(facts: Facts, inst: Instance) -> Features:
    """The features a text written from ``facts`` must have."""
    amounts = set()
    if facts.limit is not None:
        amounts.add(facts.limit)
    if facts.org_cap is not None:
        amounts.add(facts.org_cap)
    return Features(
        amounts=frozenset(amounts),
        approval=facts.states_approval(),
        permission=facts.limit is not None and facts.may_exceed,
        injected=facts.injected is not None,
        ids=frozenset() if facts.wide else frozenset(inst.allowed),
    )
