"""Surface text: each agent words the task from its beliefs (ARCHITECTURE.md §13.1).

Rendering picks a frame for the agent's role and phrase variants for each clause from the
scenario and the shared pools. "Hard" style uses the heavy-rewording pools (hard
negatives, §13.6 item 3). Every choice comes from the chain's seeded RNG, so a chain is
reproducible from ``(generator_version, seed)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from driftchain_bench.scenarios import phrases

if TYPE_CHECKING:
    from random import Random

    from driftchain_bench.facts import Facts, Instance
    from driftchain_bench.scenarios import Scenario

INLINE_SHARE = 0.7  # regular style: the limit as a trailing clause rather than a sentence


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] if text[:1].islower() else text


def render(
    scenario: Scenario, role: str, facts: Facts, inst: Instance, rng: Random, *, hard: bool
) -> str:
    p = phrases()
    words = scenario.words
    frame = rng.choice(scenario.frames["summary" if facts.compressed else role])
    pool = scenario.subject.wide if facts.wide else scenario.subject.narrow
    subject = rng.choice(pool).format(**inst.ids)

    inline = ""
    sentences: list[str] = []
    if facts.limit is not None:
        limit = scenario.fmt(facts.limit)
        if facts.may_exceed:
            if rng.random() < 0.5:
                inline = rng.choice(p.flip_inline).format(limit=limit)
            else:
                sentences.append(rng.choice(p.flip_sentence).format(limit=limit))
        elif hard:
            sentences.append(rng.choice(p.limit_hard).format(limit=limit, verb=words["verb"]))
        elif rng.random() < INLINE_SHARE:
            inline = rng.choice(p.limit_inline).format(limit=limit)
        else:
            sentences.append(rng.choice(p.limit_sentence).format(limit=limit))
        if facts.states_approval():
            qpool = p.qualifier_hard if hard else p.qualifier_regular
            sentences.append(rng.choice(qpool).format(limit=limit, approver=words["approver"]))
    if facts.org_cap is not None:
        sentences.append(
            rng.choice(p.org).format(
                noun=words["noun"], nouns=words["nouns"], org_cap=scenario.fmt(facts.org_cap)
            )
        )
    if facts.injected:
        sentences.append(facts.injected)
    text = frame.format(subject=subject, limit=inline)
    return " ".join([_sentence(text), *(_sentence(s) for s in sentences)])
