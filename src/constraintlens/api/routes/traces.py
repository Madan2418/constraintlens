"""Trace and checkpoint endpoints (ARCHITECTURE.md §11.1, §11.2)."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from constraintlens.api.deps import Authenticated, ContainerDep
from constraintlens.api.wiring import build_checker
from constraintlens.application.open_trace import OpenTraceRequest
from constraintlens.application.trace_queries import load_trace, summarise
from constraintlens.domain.enforcement import Policy
from constraintlens.domain.errors import InvalidEnvelope
from constraintlens.domain.models import (
    Attribution,
    CheckpointRequest,
    ConstraintSpec,
    Envelope,
    Event,
    RiskClass,
    TierId,
    Verdict,
)

router = APIRouter(prefix="/v1", dependencies=[Authenticated], tags=["traces"])
REPLAYED_HEADER = "Idempotent-Replayed"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenTraceBody(_Body):
    instruction: str = Field(min_length=1, max_length=20_000)
    owner: str = Field(min_length=1)
    declared: list[ConstraintSpec] = Field(default_factory=list)  # org / system policies
    risk_class: RiskClass = "high"


class OpenTraceResponse(BaseModel):
    trace_id: UUID
    envelope: Envelope
    conflicts: list[dict[str, Any]]
    extraction: dict[str, Any]


class ReplayBody(_Body):
    tiers: list[TierId] | None = None  # e.g. ["T0"] to ablate Tier 1a
    fail_high_risk: str | None = None
    fail_low_risk: str | None = None
    max_regrounds: int | None = Field(default=None, ge=0)
    escalate_approvals: bool | None = None
    theta: float | None = Field(default=None, ge=0.0, le=1.0)  # attribution (§7.7)
    delta_min: float | None = Field(default=None, ge=0.0, le=1.0)


class AttributionResponse(BaseModel):
    trace_id: UUID
    status: str
    outcome: str
    params: dict[str, float]
    through_event_id: int
    attributions: list[Attribution]
    #: What was recorded when the trace closed (ADR 0009); empty while it is open.
    recorded: list[Attribution]


@router.post("/traces", status_code=201)
async def open_trace(body: OpenTraceBody, c: ContainerDep) -> OpenTraceResponse:
    result = await c.open_trace(
        OpenTraceRequest(
            instruction=body.instruction,
            owner=body.owner,
            declared=body.declared,
            risk_class=body.risk_class,
        )
    )
    ex = result.extraction
    return OpenTraceResponse(
        trace_id=result.trace_id,
        envelope=result.envelope,
        conflicts=[x.model_dump(mode="json") for x in result.composition.conflicts],
        extraction={
            "sources": ex.sources,
            "flags": [f.model_dump(mode="json") for f in ex.flags],
            "llm_called": ex.llm_called,
            "degraded": ex.degraded,
        },
    )


@router.post("/traces/{trace_id}/checkpoints")
async def submit_checkpoint(
    trace_id: UUID,
    body: CheckpointRequest,
    c: ContainerDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
) -> Verdict:
    if body.trace_id != trace_id:
        raise InvalidEnvelope("trace_id in the path and the body differ")
    outcome = await c.verify.run(body, idempotency_key=idempotency_key)
    # §9.6: tells the SDK this is the stored verdict of an identical earlier request,
    # so a redelivered tool call is not executed a second time (ADR 0006).
    response.headers[REPLAYED_HEADER] = "true" if outcome.replayed else "false"
    return outcome.verdict


@router.post("/traces/{trace_id}/close")
async def close_trace(trace_id: UUID, c: ContainerDep) -> dict[str, Any]:
    return summarise(await c.close(trace_id))


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: UUID, c: ContainerDep) -> dict[str, Any]:
    return summarise(await load_trace(c.store, trace_id))


@router.get("/traces/{trace_id}/events")
async def get_events(
    trace_id: UUID,
    c: ContainerDep,
    after_event_id: Annotated[int, Query(ge=0)] = 0,
) -> list[Event]:
    await load_trace(c.store, trace_id)  # 404 for unknown traces
    return await c.store.read_after(after_event_id, trace_id=trace_id)


@router.get("/traces/{trace_id}/attribution")
async def get_attribution(
    trace_id: UUID,
    c: ContainerDep,
    theta: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
    delta_min: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
) -> AttributionResponse:
    """Culprit hop per failed constraint, computed now from the log (§7.7, §11.1)."""
    state = await load_trace(c.store, trace_id)
    report = await c.attribute(
        trace_id, params=c.attribute.with_overrides(theta=theta, delta_min=delta_min)
    )
    return AttributionResponse(
        trace_id=trace_id,
        status=state.status,
        outcome=state.outcome,
        params=report.params.as_dict(),
        through_event_id=report.through_event_id,
        attributions=report.attributions,
        recorded=state.attributions,
    )


@router.post("/traces/{trace_id}/replay")
async def replay_trace(
    trace_id: UUID, c: ContainerDep, body: ReplayBody | None = None
) -> dict[str, Any]:
    body = body or ReplayBody()
    s = c.settings
    try:
        policy = Policy(
            fail_high_risk=body.fail_high_risk or s.fail_high_risk,  # type: ignore[arg-type]
            fail_low_risk=body.fail_low_risk or s.fail_low_risk,  # type: ignore[arg-type]
            max_regrounds=s.max_regrounds if body.max_regrounds is None else body.max_regrounds,
            escalate_approvals=(
                s.escalate_approvals if body.escalate_approvals is None else body.escalate_approvals
            ),
        )
        checker = (
            build_checker(c.clock, body.tiers, c.replay_deps) if body.tiers else c.replay_checker
        )
    except ValueError as exc:
        raise InvalidEnvelope(str(exc)) from exc
    items = await c.replay(trace_id, checker=checker, policy=policy)
    params = c.attribute.with_overrides(theta=body.theta, delta_min=body.delta_min)
    original = await c.attribute(trace_id, params=params)
    replayed = await c.attribute(
        trace_id,
        params=params,
        outcomes={i.event_id: i.outcome for i in items if i.outcome is not None},
    )
    return {
        "trace_id": str(trace_id),
        "tiers": checker.tier_names,
        "changed": sum(i.changed for i in items),
        "items": [
            {
                "hop_id": i.hop_id,
                "checkpoint": i.checkpoint,
                "original_decision": i.original_decision,
                "replayed_decision": i.replayed_decision,
                "original_held": i.original_held,
                "replayed_held": i.replayed_held,
                "changed": i.changed,
                "reason": i.replayed_reason,
            }
            for i in items
        ],
        "attribution": {
            "params": params.as_dict(),
            "original": [a.model_dump(mode="json") for a in original.attributions],
            "replayed": [a.model_dump(mode="json") for a in replayed.attributions],
        },
    }
