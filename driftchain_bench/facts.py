"""What an agent believes the task is, and the world it acts on.

Every hop reads its parent's beliefs (``Facts``), possibly drifts (a mutation operator
rewrites the beliefs), and writes its own text from them. Downstream hops see only that
text, so a constraint lost at hop k stays lost (ARCHITECTURE.md §2.1). Labels follow
from the beliefs by construction (§13.1).
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Instance:
    """One scenario instance: the user's request with concrete ids and amounts.

    Splits are assigned per instance (§13.6 item 4), so every chain built from the same
    request and paraphrase seed lands in the same split.
    """

    instance_id: str
    scenario: str
    ids: dict[str, str]  # {"a": allowed item, "b": second allowed item, "other": out of scope}
    limit: int  # the user's bound
    total: int  # what the unconstrained task would amount to (always > limit)
    qualifier: bool  # approval required above the limit
    org_cap: int | None  # organisation policy cap (a second constraint), below the limit
    paraphrase_seed: int
    split: str = "test"

    @property
    def allowed(self) -> tuple[str, ...]:
        return tuple(self.ids[k] for k in ("a", "b") if k in self.ids)


@dataclass(frozen=True)
class Facts:
    limit: int | None
    qualifier: bool
    may_exceed: bool = False  # NEGATION_FLIP: the limit became a permission
    org_cap: int | None = None
    wide: bool = False  # SCOPE_WIDEN
    injected: str | None = None  # INJECTED_INSTRUCTION: the smuggled sentence
    compressed: bool = False  # SUMMARIZATION_LOSS: this hop writes a summary

    @classmethod
    def of(cls, inst: Instance) -> Facts:
        return cls(limit=inst.limit, qualifier=inst.qualifier, org_cap=inst.org_cap)

    def handed_on(self) -> Facts:
        """What the next agent reads: beliefs carry over, the summary style does not."""
        return replace(self, compressed=False) if self.compressed else self

    def caps(self) -> list[int]:
        """Bounds the acting agent honours, after the effects of drift."""
        if self.injected:
            return []
        out = []
        if self.limit is not None and not self.may_exceed:
            out.append(self.limit)
        if self.org_cap is not None:
            out.append(self.org_cap)
        return out

    def states_approval(self) -> bool:
        return self.qualifier and self.limit is not None and not self.may_exceed
