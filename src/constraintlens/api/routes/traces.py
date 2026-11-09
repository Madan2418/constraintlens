"""Trace and checkpoint endpoints (ARCHITECTURE.md §11.1, §11.2)."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from constraintlens.api.deps import Authenticated, ContainerDep
from constraintlens.api.wiring import build_checker
from constraintlens.application.open_trace import OpenTraceRequest
from constraintlens.application.trace_queries import load_trace, summarise
from constraintlens.domain.enforcement import Policy
from constraintlens.domain.errors import InvalidEnvelope
from constraintlens.domain.models import (
    CheckpointRequest,
    ConstraintSpec,
    Envelope,
    Event,
    RiskClass,
    TierId,
    Verdict,
)

router = APIRouter(prefix="/v1", dependencies=[Authenticated], tags=["traces"])


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
    idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
) -> Verdict:
    if body.trace_id != trace_id:
        raise InvalidEnvelope("trace_id in the path and the body differ")
    return await c.verify(body, idempotency_key=idempotency_key)


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
        checker = build_checker(c.clock, body.tiers)
    except ValueError as exc:
        raise InvalidEnvelope(str(exc)) from exc
    items = await c.replay(trace_id, checker=checker, policy=policy)
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
                "changed": i.changed,
                "reason": i.replayed_reason,
            }
            for i in items
        ],
    }
