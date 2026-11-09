"""Builders for an in-process service: app, SDK client, and a scripted drift tier."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from constraintlens.adapters.clock import FrozenClock
from constraintlens.adapters.store.sqlite import SQLiteEventStore
from constraintlens.api.app import create_app
from constraintlens.api.wiring import Container
from constraintlens.config import Settings
from constraintlens.domain.checker.pipeline import Finding, TieredChecker, TierOutcome
from constraintlens.domain.checker.tier0_integrity import IntegrityTier
from constraintlens.domain.checker.tier1a_action import ActionTier
from constraintlens.domain.models import Failure, TierId
from constraintlens.sdk import ConstraintLens
from tests.factories import T0

if TYPE_CHECKING:
    from fastapi import FastAPI

    from constraintlens.domain.checker.pipeline import CheckContext
    from constraintlens.domain.ports import EventStore

WORKED = "Refund this customer, up to $100. Anything above needs manager approval."
OPEN_STORES: list[SQLiteEventStore] = []  # stores build_app created itself


class KeywordDriftTier:
    """Stand-in for the Phase 5 semantic tiers: flags drift when T_k lacks a keyword."""

    name: TierId = "T1b"

    def __init__(self, keyword: str = "$100") -> None:
        self.keyword = keyword

    def applies(self, ctx: CheckContext) -> bool:
        return ctx.request.checkpoint in {"PUBLISH", "CONSUME"} and bool(ctx.registered)

    async def run(self, ctx: CheckContext) -> TierOutcome:
        if self.keyword in ctx.request.envelope.payload_text:
            return TierOutcome()
        c = ctx.registered[0]
        return TierOutcome(
            findings=[
                Finding(Failure(kind="TEXTUAL_DRIFT", constraint_id=c.constraint_id,
                                detail=f"{self.keyword!r} missing from task text"))
            ]
        )  # fmt: skip


class ScriptedSemanticTier:
    """Stand-in for Tier 1c / 2 (Phase 5): a *semantic* tier that can re-verify stale
    constraints (§9.3). Entailed iff ``keyword`` is in T_k; ``available=False`` models an
    unavailable model (degraded for every constraint)."""

    name: TierId = "T1c"
    semantic = True

    def __init__(self, keyword: str = "$100", *, available: bool = True) -> None:
        self.keyword = keyword
        self.available = available

    def applies(self, ctx: CheckContext) -> bool:
        return ctx.request.checkpoint in {"PUBLISH", "CONSUME"} and bool(ctx.registered)

    async def run(self, ctx: CheckContext) -> TierOutcome:
        ids = [c.constraint_id for c in ctx.registered]
        if not self.available:
            return TierOutcome(degraded=True, degraded_constraint_ids=ids)
        if self.keyword in ctx.request.envelope.payload_text:
            return TierOutcome(scores=dict.fromkeys(ids, 0.95))
        return TierOutcome(
            scores=dict.fromkeys(ids, 0.1),
            findings=[
                Finding(Failure(kind="TEXTUAL_DRIFT", constraint_id=cid,
                                detail=f"{self.keyword!r} no longer entailed"))
                for cid in ids
            ],
        )  # fmt: skip


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


async def build_app(
    *,
    store: EventStore | None = None,
    drift_tier: bool = False,
    semantic_tier: ScriptedSemanticTier | None = None,
    clock: FrozenClock | None = None,
    **overrides: Any,
) -> tuple[FastAPI, Container]:
    clock = clock or FrozenClock(T0)
    if store is None:
        store = await SQLiteEventStore("sqlite:///:memory:").initialize()
        OPEN_STORES.append(store)  # closed after the test (tests/conftest.py)
    tiers: list[Any] = [IntegrityTier(), ActionTier()]
    if drift_tier:
        tiers.append(KeywordDriftTier())
    if semantic_tier is not None:
        tiers.append(semantic_tier)
    container = Container.build(
        settings(**overrides), store=store, clock=clock, checker=TieredChecker(tiers, clock)
    )
    return create_app(container=container), container


def http_client(app: FastAPI, **headers: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cl.test", headers=headers
    )


def sdk_client(app: FastAPI, **kw: Any) -> ConstraintLens:
    return ConstraintLens("http://cl.test", transport=httpx.ASGITransport(app=app), **kw)
