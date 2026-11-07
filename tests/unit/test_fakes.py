"""Behaviour specific to individual fakes (shared port behaviour lives in tests/contract)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from constraintlens.adapters.clock import FrozenClock
from constraintlens.adapters.judge.scripted import ScriptedJudge
from constraintlens.adapters.models.fakes import HashEmbedder, ScriptedNLI
from constraintlens.adapters.transport.memory import InMemoryQueue
from constraintlens.domain.errors import DependencyUnavailable
from constraintlens.domain.models import JudgeResult
from tests.factories import make_envelope


class TestFrozenClock:
    def test_advance_moves_both_clocks(self) -> None:
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        clock.advance(90)
        assert clock.now() == datetime(2026, 1, 1, 0, 1, 30, tzinfo=UTC)
        assert clock.monotonic() == 90

    def test_rejects_naive_start_and_backwards_time(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            FrozenClock(datetime(2026, 1, 1))
        with pytest.raises(ValueError, match="backwards"):
            FrozenClock().advance(-1)


class TestScriptedJudge:
    async def test_scripted_and_default_responses(self) -> None:
        hit = JudgeResult(verdict="CONTRADICTED", reason="limit removed", confidence=0.9)
        judge = ScriptedJudge({("C", "T"): hit})
        assert await judge.judge(canonical_text="C", task_text="T") == hit
        miss = await judge.judge(canonical_text="C", task_text="other")
        assert miss.verdict == "AMBIGUOUS"
        assert [c.task_text for c in judge.calls] == ["T", "other"]

    async def test_failure_injection(self) -> None:
        judge = ScriptedJudge(fail_with=DependencyUnavailable("judge", "timeout"))
        with pytest.raises(DependencyUnavailable):
            await judge.judge(canonical_text="C", task_text="T")
        assert len(judge.calls) == 1


class TestScriptedNLI:
    def test_scores_and_failure(self) -> None:
        nli = ScriptedNLI({("p", "h"): 0.9}, default=0.1)
        assert nli.entailment("p", "h") == 0.9
        assert nli.entailment("p", "x") == 0.1
        nli.fail_with = DependencyUnavailable("nli")
        with pytest.raises(DependencyUnavailable):
            nli.entailment("p", "h")

    def test_rejects_out_of_range_scores(self) -> None:
        with pytest.raises(ValueError, match="outside"):
            ScriptedNLI(default=1.5)


class TestHashEmbedder:
    def test_empty_text_still_unit_vector(self) -> None:
        (vec,) = HashEmbedder(dimension=8).encode([""])
        assert vec == [1.0] + [0.0] * 7

    def test_rejects_bad_dimension(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            HashEmbedder(dimension=0)


class TestInMemoryQueue:
    async def test_reclaim_respects_idle_time_and_counts_redeliveries(self) -> None:
        clock = FrozenClock()
        q = InMemoryQueue(clock)
        await q.ensure_group("tasks", "g")
        await q.publish("tasks", make_envelope())
        (first,) = await q.consume("tasks", "g", "worker-a")
        assert first.delivery_count == 1

        assert await q.reclaim("tasks", "g", "worker-b", min_idle_ms=1000) == []
        clock.advance(2)
        (again,) = await q.reclaim("tasks", "g", "worker-b", min_idle_ms=1000)
        assert again.message_id == first.message_id
        assert again.delivery_count == 2

        await q.ack("tasks", "g", again.message_id)
        assert q.pending_count("tasks", "g") == 0
        clock.advance(10)
        assert await q.reclaim("tasks", "g", "worker-b", min_idle_ms=0) == []

    async def test_unknown_group_raises(self) -> None:
        q = InMemoryQueue()
        with pytest.raises(LookupError, match="does not exist"):
            await q.consume("tasks", "nope", "w")
