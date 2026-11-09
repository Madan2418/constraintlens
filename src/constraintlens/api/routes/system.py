"""Health and metrics (ARCHITECTURE.md §11.1, §22)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from constraintlens import __version__
from constraintlens.api.deps import Authenticated, ContainerDep
from constraintlens.api.wiring import TIER_FACTORIES
from constraintlens.application.trace_queries import metrics as compute_metrics

router = APIRouter(prefix="/v1", tags=["system"])


@router.get("/health")
async def health(c: ContainerDep) -> dict[str, Any]:
    """Liveness and dependency status. Unauthenticated so orchestrators can probe it."""
    try:
        await c.store.read_after(0, limit=1)
        store = "ok"
    except Exception as exc:  # report, do not raise: health must always answer
        store = f"unavailable: {exc.__class__.__name__}"
    return {
        "status": "ok" if store == "ok" else "degraded",
        "version": __version__,
        "dependencies": {
            "store": store,
            "judge": "configured" if c.judge is not None else "not configured (rules only)",
            "redis": "not used by the service: pipelines reach it through the SDK (§6.1)",
            "models": "none loaded (Tier 1c arrives in Phase 5)",
        },
        "tiers": list(TIER_FACTORIES),
        "async_guard": {
            "dwell_threshold_s": c.settings.dwell_threshold_s,
            "join_timeout_s": c.settings.join_timeout_s,
            "consume_timeout_s": c.settings.consume_timeout_s,
            "sweeper": "on" if c.settings.sweep_interval_s else "off",
        },
    }


@router.get("/metrics", dependencies=[Authenticated])
async def metrics(c: ContainerDep) -> dict[str, Any]:
    return await compute_metrics(c.store)
