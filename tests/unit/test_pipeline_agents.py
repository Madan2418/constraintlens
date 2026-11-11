"""Scripted reference agents: text round trip and mutation semantics (Phase 2)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pipelines.common.agents import VirtualClock
from pipelines.common.facts import INJECTION, MUTATIONS, TaskFacts, fmt_usd, mutate, parse
from pipelines.common.scenarios import PROCUREMENT, REFUND, SCENARIOS, Scenario


@st.composite
def facts(draw: st.DrawFn) -> TaskFacts:
    limit = draw(st.none() | st.integers(1, 100_000).map(Decimal))
    return TaskFacts(
        limit=limit,
        approval_above=draw(st.booleans()),
        may_exceed=draw(st.booleans()) if limit is not None else False,
        org_cap=draw(st.none() | st.integers(1, 100_000).map(Decimal)),
        wide_scope=draw(st.booleans()),
        injected=draw(st.sampled_from([None, INJECTION])),
    )


@pytest.mark.parametrize("scenario", [REFUND, PROCUREMENT], ids=lambda s: s.name)
@given(f=facts())
@settings(deadline=None)  # pure parsing; a wall-clock deadline only flakes on a loaded host
def test_relay_agents_preserve_what_they_read(scenario: Scenario, f: TaskFacts) -> None:
    """Hops 1 and 2 paraphrase without changing meaning unless a mutation is applied."""
    for role in scenario.roles[:2]:
        assert parse(scenario.render(role, f), wide_phrase=scenario.wide_phrase) == f


@pytest.mark.parametrize("scenario", [REFUND, PROCUREMENT], ids=lambda s: s.name)
def test_user_instruction_is_read_correctly(scenario: Scenario) -> None:
    plain = parse(scenario.instruction(), wide_phrase=scenario.wide_phrase)
    assert plain.limit is not None
    assert plain.approval_above
    assert plain.org_cap is None
    with_org = parse(scenario.instruction(org_policy=True), wide_phrase=scenario.wide_phrase)
    assert with_org.org_cap == scenario.org_cap
    assert with_org.limit == plain.limit  # the policy amount is not mistaken for the limit


def test_mutations() -> None:
    base = TaskFacts(limit=Decimal(100), approval_above=True, org_cap=Decimal(50))
    assert mutate(base, "NONE") == base
    assert mutate(base, "NUMERIC_LOOSEN").limit == Decimal(1000)
    assert not mutate(base, "QUALIFIER_DELETE").approval_above
    assert mutate(base, "NEGATION_FLIP").caps() == [Decimal(50)]
    assert mutate(base, "SCOPE_WIDEN").wide_scope
    lost = mutate(base, "SUMMARIZATION_LOSS")
    assert (lost.limit, lost.org_cap, lost.approval_above) == (None, None, False)
    assert mutate(base, "INJECTED_INSTRUCTION").caps() == []
    assert mutate(base, "CONSTRAINT_DROP_ONE").caps() == [Decimal(100)]
    assert set(MUTATIONS) >= {"NONE", "CONSTRAINT_DROP_ONE"}


def test_mutation_preconditions() -> None:
    with pytest.raises(ValueError, match="needs a limit"):
        mutate(TaskFacts(), "NUMERIC_LOOSEN")
    with pytest.raises(ValueError, match="org policy"):
        mutate(TaskFacts(limit=Decimal(1)), "CONSTRAINT_DROP_ONE")
    with pytest.raises(ValueError, match="unknown mutation"):
        mutate(TaskFacts(), "BOGUS")  # type: ignore[arg-type]


def test_helpers() -> None:
    assert fmt_usd(Decimal(15000)) == "$15,000"
    assert fmt_usd(Decimal("2.5")) == "$2.50"
    clock = VirtualClock()
    start = clock.now()
    clock.advance(720)
    assert (clock.now() - start).total_seconds() == 720
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)
    assert set(SCENARIOS) == {"refund", "procurement"}
