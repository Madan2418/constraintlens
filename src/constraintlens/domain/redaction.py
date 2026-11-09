"""Redaction of personal data before persistence (ARCHITECTURE.md §12.4, §21.1).

Regex masks for e-mail addresses, payment-card numbers and phone numbers, applied to
task text before it reaches the event log unless ``store_full_text`` is opted in.
Order matters: cards are masked before phones, so a card number is not half-read as
a phone number.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),
    "card": re.compile(r"\b(?:\d[ -]?){12,18}\d\b"),
    "phone": re.compile(r"(?<![\w#])\+?\d[\d\s().-]{7,}\d\b"),
}
ORDER = ("email", "card", "phone")


def redact(text: str, kinds: Iterable[str] = ORDER) -> str:
    wanted = set(kinds)
    unknown = wanted - set(PATTERNS)
    if unknown:
        raise ValueError(f"unknown redaction pattern(s): {sorted(unknown)}")
    for kind in ORDER:
        if kind in wanted:
            text = PATTERNS[kind].sub(f"[{kind.upper()}]", text)
    return text
