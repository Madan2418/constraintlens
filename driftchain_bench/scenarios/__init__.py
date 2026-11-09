"""Scenario templates (ARCHITECTURE.md §13.2), loaded from the YAML files in this folder.

A template describes one kind of delegated task: the constrained action and its unit, the
subject the user allowed (an order, two counterparties, a dataset ...), the tool the last
agent calls, the agent roles, and how each role words the task. Adding a scenario means
adding a YAML file; no code changes (open/closed, §5).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

SCENARIO_DIR = Path(__file__).parent
ROLE_FRAMES = ("user", "summary")


@dataclass(frozen=True)
class SubjectTemplate:
    kind: str  # subject kind in the constraint, e.g. "order" -> "order:4471"
    arg: str  # tool argument naming the subject item
    items: int  # how many items the user allowed (1 or 2)
    narrow: tuple[str, ...]  # phrases naming the allowed items ({a}, {b})
    wide: tuple[str, ...]  # SCOPE_WIDEN phrases (name no item)
    id_format: str = "{n}"
    id_range: tuple[int, int] = (1000, 9999)
    wide_format: str | None = None  # format of the out-of-scope item; default id_format
    id_pool: tuple[str, ...] = ()  # names instead of numbers (procurement vendors)


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    action: str
    resource: str
    unit: str
    risk_class: str
    owner: str
    tool: str
    amount_arg: str
    subject: SubjectTemplate
    limits: tuple[int, ...]
    words: dict[str, str]
    root: str
    middle: tuple[str, str]
    actor: str
    side: str
    frames: dict[str, tuple[str, ...]]

    def fmt(self, amount: int) -> str:
        """``$1,000`` for money, ``200 records`` for counts."""
        if self.unit == "USD":
            return f"${amount:,}"
        return f"{amount:,} {self.unit}"


@dataclass(frozen=True)
class Phrases:
    limit_inline: tuple[str, ...]
    limit_sentence: tuple[str, ...]
    limit_hard: tuple[str, ...]
    flip_inline: tuple[str, ...]
    flip_sentence: tuple[str, ...]
    qualifier_regular: tuple[str, ...]
    qualifier_hard: tuple[str, ...]
    org: tuple[str, ...]
    injection_regular: tuple[str, ...]
    injection_adversarial: tuple[str, ...]


def _t(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        raise ValueError(f"expected a non-empty list of strings, got {values!r}")
    return tuple(values)


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a mapping")
    return data


def _scenario(data: dict[str, Any]) -> Scenario:
    s = data["subject"]
    roles = data["roles"]
    subject = SubjectTemplate(
        kind=s["kind"],
        arg=s["arg"],
        items=int(s["items"]),
        narrow=_t(s["narrow"]),
        wide=_t(s["wide"]),
        id_format=s.get("id_format", "{n}"),
        id_range=(int(s.get("id_range", (1000, 9999))[0]), int(s.get("id_range", (1000, 9999))[1])),
        wide_format=s.get("wide_format"),
        id_pool=tuple(s.get("id_pool", ())),
    )
    if subject.items not in (1, 2):
        raise ValueError(f"{data['name']}: subject.items must be 1 or 2")
    frames = {k: _t(v) for k, v in data["frames"].items()}
    middle = tuple(roles["middle"])
    if len(middle) != 2:
        raise ValueError(f"{data['name']}: roles.middle needs exactly two roles")
    needed = {*ROLE_FRAMES, roles["root"], *middle, roles["actor"], roles["side"]}
    missing = needed - set(frames)
    if missing:
        raise ValueError(f"{data['name']}: no frames for {sorted(missing)}")
    for role, pool in frames.items():
        for frame in pool:
            if "{subject}" not in frame:
                raise ValueError(f"{data['name']}.{role}: frame without {{subject}}: {frame!r}")
            if role != "summary" and "{limit}" not in frame:
                raise ValueError(f"{data['name']}.{role}: frame without {{limit}}: {frame!r}")
            if role == "summary" and "{limit}" in frame:
                raise ValueError(f"{data['name']}: summary frames must not state the limit")
    for phrase in subject.narrow:
        for slot in ("{a}", "{b}")[: subject.items]:
            if slot not in phrase:
                raise ValueError(f"{data['name']}: narrow phrase {phrase!r} lacks {slot}")
    limits = tuple(int(v) for v in data["limits"])
    if any(v <= 0 or v % 2 for v in limits):
        raise ValueError(f"{data['name']}: limits must be positive and even (org cap = half)")
    return Scenario(
        name=data["name"],
        title=data["title"],
        action=data["action"],
        resource=data["resource"],
        unit=data["unit"],
        risk_class=data["risk_class"],
        owner=data["owner"],
        tool=data["tool"],
        amount_arg=data["amount_arg"],
        subject=subject,
        limits=limits,
        words={k: str(v) for k, v in data["words"].items()},
        root=roles["root"],
        middle=(middle[0], middle[1]),
        actor=roles["actor"],
        side=roles["side"],
        frames=frames,
    )


@cache
def phrases() -> Phrases:
    d = _load(SCENARIO_DIR / "_common.yaml")
    return Phrases(
        limit_inline=_t(d["limit"]["inline"]),
        limit_sentence=_t(d["limit"]["sentence"]),
        limit_hard=_t(d["limit"]["hard"]),
        flip_inline=_t(d["flip"]["inline"]),
        flip_sentence=_t(d["flip"]["sentence"]),
        qualifier_regular=_t(d["qualifier"]["regular"]),
        qualifier_hard=_t(d["qualifier"]["hard"]),
        org=_t(d["org"]),
        injection_regular=_t(d["injection"]["regular"]),
        injection_adversarial=_t(d["injection"]["adversarial"]),
    )


@cache
def load_scenarios() -> dict[str, Scenario]:
    """Every ``*.yaml`` template except ``_common.yaml``, keyed by name, in name order."""
    out: dict[str, Scenario] = {}
    for path in sorted(SCENARIO_DIR.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        scn = _scenario(_load(path))
        if scn.name != path.stem:
            raise ValueError(f"{path.name}: name {scn.name!r} does not match the file name")
        out[scn.name] = scn
    return out
