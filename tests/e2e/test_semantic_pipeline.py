"""Phase 5 end to end: the refund pipeline against a service with every tier wired.

The service is built the production way (``Container.build`` picks the tiers from the
configured models), with deterministic stand-ins for the NLI model and the judge. It
checks the §2.1 demo path (the drift is flagged at the hop that introduced it, before
any tool runs) and the judge-failure path (§10.1) end to end, through the SDK.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
import pytest

from constraintlens.adapters.judge.anthropic import AnthropicJudge
from constraintlens.adapters.judge.scripted import ScriptedJudge
from constraintlens.adapters.models.fakes import ScriptedNLI
from constraintlens.adapters.store.sqlite import SQLiteEventStore
from constraintlens.api.app import create_app
from constraintlens.api.wiring import Container
from constraintlens.domain.models import JudgeResult
from constraintlens.sdk import ConstraintLens
from pipelines import refund_langgraph as protected
from tests.contract.test_judge import fake_anthropic_client, message
from tests.service_helpers import settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ENTAILED = JudgeResult(verdict="ENTAILED", reason="kept", confidence=0.9, evidence="$100")


async def service(**deps: Any) -> tuple[ConstraintLens, SQLiteEventStore, Container]:
    store = await SQLiteEventStore("sqlite:///:memory:").initialize()
    overrides = deps.pop("settings", {})
    container = Container.build(
        settings(**overrides), store=store, judge_cache=store.judge_cache(), **deps
    )
    app = create_app(container=container)
    cl = ConstraintLens("http://cl.test", transport=httpx.ASGITransport(app=app))
    return cl, store, container


@pytest.fixture
async def full() -> AsyncIterator[tuple[ConstraintLens, SQLiteEventStore, Container]]:
    cl, store, c = await service(
        nli=ScriptedNLI(default=0.92), judge=ScriptedJudge(default=ENTAILED)
    )
    yield cl, store, c
    await cl.aclose()
    await store.close()


async def test_every_tier_is_wired_and_reported(full: Any) -> None:
    cl, _, _ = full
    health = (await cl._http.get("/v1/health")).json()
    assert health["tiers"] == ["T0", "T1a", "T1b", "T1c", "T2"]
    assert health["dependencies"]["judge"].startswith("configured (scripted")
    assert health["dependencies"]["models"]["nli"] == "loaded"
    assert health["dependencies"]["judge_cache"] == "on"


async def test_clean_run_passes_every_tier(full: Any) -> None:
    cl, _, _ = full
    res = await protected.run(cl)
    trace = await cl.get_trace(UUID(res.trace_id))
    assert [c.tool for c in res.tool_calls] == ["issue_refund", "request_manager_approval"]
    assert {v["decision"] for v in trace["verdicts"]} == {"ALLOW"}
    publish = [v for v in trace["verdicts"] if v["checkpoint"] == "PUBLISH"]
    assert {v["tier_used"] for v in publish} == {"T1c"}  # entailed at the NLI tier


async def test_worked_example_is_flagged_at_the_triage_hop(full: Any) -> None:
    cl, _, _ = full
    res = await protected.run(cl, "SUMMARIZATION_LOSS", 2)
    trace = await cl.get_trace(UUID(res.trace_id))
    assert res.tool_calls == []
    assert res.violations == []
    assert res.halted is not None
    triage = [v for v in trace["verdicts"] if v["hop_id"].startswith("h2")
              and v["checkpoint"] == "PUBLISH"]  # fmt: skip
    assert [(v["hop_id"], v["decision"]) for v in triage] == [
        ("h2", "RE_GROUND"), ("h2/r1", "RE_GROUND"), ("h2/r2", "BLOCK"),
    ]  # fmt: skip
    assert triage[0]["tier_used"] == "T1b"
    assert "limit 100 USD missing" in triage[0]["failures"][0]["detail"]
    # The intake hop, upstream of the drift, is clean: the culprit hop is visible.
    assert all(v["decision"] == "ALLOW" for v in trace["verdicts"] if v["hop_id"] == "h1")


@pytest.mark.parametrize(
    ("judge", "overrides", "detail"),
    [
        (ScriptedJudge(fail_with=ConnectionError("judge API down")), {}, "judge API down"),
        (AnthropicJudge(client=fake_anthropic_client(message("I think it's fine"))), {},
         "invalid output after one retry"),
    ],
)  # fmt: skip
async def test_judge_failure_fails_closed_for_high_risk(
    judge: Any, overrides: dict[str, Any], detail: str
) -> None:
    # NLI sits in the uncertainty band, so every text checkpoint needs the judge.
    cl, store, _ = await service(nli=ScriptedNLI(default=0.5), judge=judge, settings=overrides)
    res = await protected.run(cl)
    trace = await cl.get_trace(UUID(res.trace_id))
    first = trace["verdicts"][0]
    assert (first["hop_id"], first["decision"], first["degraded"]) == ("h1", "BLOCK", True)
    assert res.tool_calls == []
    assert res.halted is not None
    events = await cl.events(UUID(res.trace_id))
    failures = [e for e in events if e["type"] == "JUDGE_FAILED"]
    assert failures
    assert detail in failures[0]["payload"]["detail"]
    metrics = (await cl._http.get("/v1/metrics")).json()
    assert metrics["judge_failures"] >= 1
    await cl.aclose()
    await store.close()


async def test_judge_failure_fails_open_for_low_risk() -> None:
    cl, store, _ = await service(
        nli=ScriptedNLI(default=0.5), judge=ScriptedJudge(fail_with=TimeoutError())
    )
    opened = await cl.open_trace(protected.baseline.REFUND.instruction(), owner="u",
                                 risk_class="low")  # fmt: skip
    vague = "Refund order #4471 within the usual limit; approval above it."  # passes T1b
    verdict = await cl.publish(opened.envelope.model_copy(update={"payload_text": vague}))
    assert (verdict.decision, verdict.degraded, verdict.tier_used) == (
        "ALLOW_WITH_WARNING", True, "T2",
    )  # fmt: skip
    await cl.aclose()
    await store.close()


async def test_judge_cache_serves_repeated_checks_and_replay_spends_nothing() -> None:
    judge = ScriptedJudge(default=ENTAILED)
    cl, store, _ = await service(nli=ScriptedNLI(default=0.5), judge=judge)
    first = await protected.run(cl)
    calls = len(judge.calls)
    await protected.run(cl)  # identical texts: answered from the judge cache
    assert len(judge.calls) == calls
    replay = await cl.replay(UUID(first.trace_id))
    assert replay["changed"] == 0
    assert len(judge.calls) == calls  # replay is cache-only
    metrics = (await cl._http.get("/v1/metrics")).json()
    assert metrics["cache_hit_rate"] > 0
    assert json.dumps(metrics)
    await cl.aclose()
    await store.close()
