"""Composition root: the only place concrete adapters are chosen and wired (§5, §17).

``Container`` holds the adapters and use cases for one running service. Tests build
one with fakes; ``default_container`` builds the production one from ``Settings``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from constraintlens.adapters.clock import SystemClock
from constraintlens.adapters.store.sqlite import SQLiteEventStore
from constraintlens.application.open_trace import OpenTrace
from constraintlens.application.sweep_transit import SweepTransitLoss
from constraintlens.application.trace_queries import CloseTrace, ReplayTrace
from constraintlens.application.verify_checkpoint import VerifyCheckpoint
from constraintlens.domain.async_guard import GuardConfig
from constraintlens.domain.checker.pipeline import TieredChecker
from constraintlens.domain.checker.tier0_integrity import IntegrityTier
from constraintlens.domain.checker.tier1a_action import ActionTier
from constraintlens.domain.enforcement import Policy

if TYPE_CHECKING:
    from collections.abc import Callable

    from constraintlens.config import Settings
    from constraintlens.domain.checker.pipeline import Tier
    from constraintlens.domain.models import TierId
    from constraintlens.domain.ports import Clock, EventStore, Judge


#: Tiers available in this phase, in routing order (§7.4). Phase 5 adds T1b, T1c, T2.
TIER_FACTORIES: dict[TierId, Callable[[], Tier]] = {"T0": IntegrityTier, "T1a": ActionTier}


def build_checker(clock: Clock, tiers: list[TierId] | None = None) -> TieredChecker:
    names: list[TierId] = tiers or list(TIER_FACTORIES)
    unknown = [n for n in names if n not in TIER_FACTORIES]
    if unknown:
        raise ValueError(f"unknown or unavailable tiers: {unknown}")
    # Keep routing order regardless of the order requested.
    return TieredChecker([TIER_FACTORIES[n]() for n in TIER_FACTORIES if n in names], clock)


def guard_from(settings: Settings) -> GuardConfig:
    return GuardConfig(
        dwell_threshold_s=float(settings.dwell_threshold_s),
        join_timeout_s=settings.join_timeout_s,
    )


def policy_from(settings: Settings) -> Policy:
    return Policy(
        fail_high_risk=settings.fail_high_risk,
        fail_low_risk=settings.fail_low_risk,
        max_regrounds=settings.max_regrounds,
        escalate_approvals=settings.escalate_approvals,
    )


@dataclass
class Container:
    settings: Settings
    store: EventStore
    clock: Clock
    judge: Judge | None
    open_trace: OpenTrace
    verify: VerifyCheckpoint
    close: CloseTrace
    replay: ReplayTrace
    sweep: SweepTransitLoss

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        store: EventStore,
        clock: Clock | None = None,
        judge: Judge | None = None,
        checker: TieredChecker | None = None,
    ) -> Container:
        clock = clock or SystemClock()
        return cls(
            settings=settings,
            store=store,
            clock=clock,
            judge=judge,
            open_trace=OpenTrace(
                store=store,
                clock=clock,
                judge=judge,
                llm_mode=settings.extraction_llm_mode,
                default_max_age_s=settings.default_max_age_s,
                store_full_text=settings.store_full_text,
            ),
            verify=VerifyCheckpoint(
                store=store,
                clock=clock,
                checker=checker or build_checker(clock),
                policy=policy_from(settings),
                store_full_text=settings.store_full_text,
                redact_patterns=settings.redact_pattern_list,
                guard=guard_from(settings),
            ),
            close=CloseTrace(store=store, clock=clock),
            replay=ReplayTrace(store=store, guard=guard_from(settings)),
            sweep=SweepTransitLoss(
                store=store, clock=clock, consume_timeout_s=settings.consume_timeout_s
            ),
        )


async def default_container(settings: Settings) -> Container:
    # No judge adapter yet: extraction runs rules-only until Phase 5 (ADR 0005).
    store = await SQLiteEventStore(settings.db_url).initialize()
    return Container.build(settings, store=store)
