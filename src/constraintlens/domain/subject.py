"""Subject scope grammar (ARCHITECTURE.md §7.6 "resource scope: intersection"; ADR 0003).

A constraint's ``subject`` names what it applies to, as a set of items::

    subject   := "*" | item | "{" item ("," item)* "}"
    item      := kind ":" id   |   kind ":*"   |   name
    e.g.      "*", "order:4471", "order:*", "{account:1200,account:1300}"

``"*"`` is the universe. ``kind:*`` matches every item of that kind. Items are
lower-cased. Scopes support intersection and membership, which is all that
composition (most-restrictive-wins) and the Tier 1a action check need.

Pure module: standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass

_FORBIDDEN = set("{},")


@dataclass(frozen=True)
class SubjectScope:
    """A parsed subject. ``items is None`` means the universe (``"*"``)."""

    items: frozenset[str] | None

    # ---------------------------------------------------------------- parsing

    @classmethod
    def parse(cls, text: str) -> SubjectScope:
        raw = text.strip()
        if raw == "*":
            return cls(None)
        if raw.startswith("{") and raw.endswith("}"):
            parts = [p.strip() for p in raw[1:-1].split(",")]
        else:
            parts = [raw]
        if not parts or any(not p or _FORBIDDEN & set(p) or p == "*" for p in parts):
            raise ValueError(f"malformed subject {text!r}")
        return cls(frozenset(p.lower() for p in parts))

    @classmethod
    def everything(cls) -> SubjectScope:
        return cls(None)

    def format(self) -> str:
        """Canonical text form; ``parse(format(s)) == s``."""
        if self.items is None:
            return "*"
        ordered = sorted(self.items)
        return ordered[0] if len(ordered) == 1 else "{" + ",".join(ordered) + "}"

    # ------------------------------------------------------------- semantics

    @property
    def is_universe(self) -> bool:
        return self.items is None

    @property
    def is_empty(self) -> bool:
        return self.items is not None and not self.items

    def contains(self, item: str) -> bool:
        """Whether a concrete item (e.g. ``"order:4471"``) falls inside this scope."""
        if self.items is None:
            return True
        return any(_item_covers(s, item.lower()) for s in self.items)

    def intersect(self, other: SubjectScope) -> SubjectScope:
        if self.items is None:
            return other
        if other.items is None:
            return self
        out: set[str] = set()
        for a in self.items:
            for b in other.items:
                if _item_covers(a, b):
                    out.add(b)
                elif _item_covers(b, a):
                    out.add(a)
        return SubjectScope(frozenset(out))

    def is_subset_of(self, other: SubjectScope) -> bool:
        if other.items is None:
            return True
        if self.items is None:
            return False
        return all(any(_item_covers(o, s) for o in other.items) for s in self.items)


def _item_covers(pattern: str, item: str) -> bool:
    """``order:*`` covers ``order:4471`` and ``order:*``; otherwise exact match."""
    if pattern == item:
        return True
    if pattern.endswith(":*"):
        return item.startswith(pattern[:-1])
    return False
