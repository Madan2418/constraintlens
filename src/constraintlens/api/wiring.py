"""Composition root: the only place concrete adapters are chosen and wired (§5, §17).

``Container`` holds the adapters and use cases for one running service. Tests build
one with fakes; ``default_container`` builds the production one from ``Settings``.

Tiers are registered in routing order (§7.4). A tier whose dependency is missing is
simply not registered (Tier 1c without an NLI model, Tier 2 without a judge), and
``/v1/health`` says so. Asking for an unavailable tier explicitly (e.g. in a replay)
is an error, not a silent skip.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from constraintlens.adapters.clock import SystemClock
from constraintlens.adapters.store.sqlite import SQLiteEventStore
from constraintlens.application.attribute_trace import AttributeTrace
from constraintlens.application.open_trace import OpenTrace
from constraintlens.application.sweep_transit import SweepTransitLoss
from constraintlens.application.trace_queries import CloseTrace, ReplayTrace
from constraintlens.application.verify_checkpoint import VerifyCheckpoint
from constraintlens.domain.async_guard import GuardConfig
from constraintlens.domain.attribution import AttributionParams
from constraintlens.domain.checker.pipeline import TieredChecker
from constraintlens.domain.checker.tier0_integrity import IntegrityTier
from constraintlens.domain.checker.tier1a_action import ActionTier
from constraintlens.domain.checker.tier1b_lexical import LexicalTier
from constraintlens.domain.checker.tier1c_nli import NLITier
from constraintlens.domain.checker.tier2_judge import JudgeTier
from constraintlens.domain.enforcement import Policy

if TYPE_CHECKING:
    from constraintlens.config import Settings
    from constraintlens.domain.checker.pipeline import Tier
    from constraintlens.domain.models import TierId
    from constraintlens.domain.ports import (
        Clock,
        Embedder,
        EventStore,
        Judge,
        JudgeCache,
        NLIModel,
    )

log = logging.getLogger("constraintlens.wiring")

#: Routing order (§7.4). Every checker keeps this order regardless of the request.
TIER_ORDER: tuple[TierId, ...] = ("T0", "T1a", "T1b", "T1c", "T2")


@dataclass(frozen=True)
class TierDeps:
    """What the tiers need beyond the clock. ``None`` means that tier is unavailable."""

    nli: NLIModel | None = None
    embedder: Embedder | None = None
    judge: Judge | None = None
    judge_cache: JudgeCache | None = None
    theta_low: float = 0.35
    theta_high: float = 0.80
    nli_timeout_s: float = 0.5
    irrelevant_below: float = 0.15
    judge_timeout_s: float = 10.0
    use_cache: bool = True
    call_judge: bool = True  # False: cache-only Tier 2 (replay, §11.1 "no side effects")

    def make(self, name: TierId) -> Tier | None:
        if name == "T0":
            return IntegrityTier()
        if name == "T1a":
            return ActionTier()
        if name == "T1b":
            return LexicalTier()
        if name == "T1c":
            if self.nli is None:
                return None
            return NLITier(
                self.nli, theta_low=self.theta_low, theta_high=self.theta_high,
                timeout_s=self.nli_timeout_s, embedder=self.embedder,
                irrelevant_below=self.irrelevant_below,
            )  # fmt: skip
        if name == "T2":
            if self.judge is None:
                return None
            return JudgeTier(
                self.judge, cache=self.judge_cache, timeout_s=self.judge_timeout_s,
                use_cache=self.use_cache, call_judge=self.call_judge,
            )  # fmt: skip
        raise ValueError(f"unknown tier {name!r}")  # pragma: no cover - TierId is closed

    def available(self) -> list[TierId]:
        return [n for n in TIER_ORDER if self.make(n) is not None]


def build_checker(
    clock: Clock, tiers: list[TierId] | None = None, deps: TierDeps | None = None
) -> TieredChecker:
    deps = deps or TierDeps()
    unknown = [n for n in tiers or [] if n not in TIER_ORDER]
    if unknown:
        raise ValueError(f"unknown tiers: {unknown}")
    wanted = set(tiers) if tiers else set(TIER_ORDER)
    built: list[Tier] = []
    for name in TIER_ORDER:
        if name not in wanted:
            continue
        tier = deps.make(name)
        if tier is None:
            if tiers:
                raise ValueError(
                    f"tier {name} is not available (its model or judge is not configured)"
                )
            continue
        built.append(tier)
    return TieredChecker(built, clock)


def policy_from(settings: Settings) -> Policy:
    return Policy(
        fail_high_risk=settings.fail_high_risk,
        fail_low_risk=settings.fail_low_risk,
        max_regrounds=settings.max_regrounds,
        escalate_approvals=settings.escalate_approvals,
    )


def guard_from(settings: Settings) -> GuardConfig:
    return GuardConfig(
        dwell_threshold_s=float(settings.dwell_threshold_s),
        join_timeout_s=settings.join_timeout_s,
    )


def attribution_params_from(settings: Settings) -> AttributionParams:
    """§7.7: a hop fails below ``theta_low`` (the NLI not-entailed bound); ``delta_min``
    filters noise; staleness is judged with the live guard's dwell threshold."""
    return AttributionParams(
        theta=settings.theta_low, delta_min=settings.delta_min, guard=guard_from(settings)
    )


def tier_deps_from(
    settings: Settings,
    *,
    nli: NLIModel | None = None,
    embedder: Embedder | None = None,
    judge: Judge | None = None,
    judge_cache: JudgeCache | None = None,
) -> TierDeps:
    return TierDeps(
        nli=nli if settings.enable_tier_1c else None,
        embedder=embedder,
        judge=judge if settings.enable_tier_2 else None,
        judge_cache=judge_cache,
        theta_low=settings.theta_low,
        theta_high=settings.theta_high,
        nli_timeout_s=settings.nli_timeout_s,
        irrelevant_below=settings.route_irrelevant_below,
        judge_timeout_s=settings.judge_timeout_s,
        use_cache=settings.judge_cache,
    )


@dataclass
class Container:
    settings: Settings
    store: EventStore
    clock: Clock
    judge: Judge | None
    tier_deps: TierDeps
    open_trace: OpenTrace
    verify: VerifyCheckpoint
    close: CloseTrace
    replay: ReplayTrace
    attribute: AttributeTrace
    sweep: SweepTransitLoss
    #: What a replay with no tier override re-runs: the live tiers, with Tier 2 cache-only.
    replay_checker: TieredChecker

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        store: EventStore,
        clock: Clock | None = None,
        judge: Judge | None = None,
        checker: TieredChecker | None = None,
        nli: NLIModel | None = None,
        embedder: Embedder | None = None,
        judge_cache: JudgeCache | None = None,
    ) -> Container:
        clock = clock or SystemClock()
        deps = tier_deps_from(
            settings, nli=nli, embedder=embedder, judge=judge, judge_cache=judge_cache
        )
        live = checker or build_checker(clock, deps=deps)
        # A checker passed in (tests, custom tiers) is replayed as is; the default one is
        # rebuilt with a cache-only judge so replay never spends money.
        replay_checker = checker or build_checker(clock, deps=replace(deps, call_judge=False))
        attribute = AttributeTrace(
            store=store, clock=clock, params=attribution_params_from(settings)
        )
        return cls(
            settings=settings,
            store=store,
            clock=clock,
            judge=judge,
            tier_deps=deps,
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
                checker=live,
                policy=policy_from(settings),
                store_full_text=settings.store_full_text,
                redact_patterns=settings.redact_pattern_list,
                guard=guard_from(settings),
            ),
            close=CloseTrace(store=store, clock=clock, attribute=attribute),
            replay=ReplayTrace(store=store, guard=guard_from(settings)),
            attribute=attribute,
            sweep=SweepTransitLoss(
                store=store, clock=clock, consume_timeout_s=settings.consume_timeout_s
            ),
            replay_checker=replay_checker,
        )

    @property
    def replay_deps(self) -> TierDeps:
        """Replay never calls the judge: Tier 2 answers from the cache only."""
        return replace(self.tier_deps, call_judge=False)


def judge_from(settings: Settings) -> Judge | None:
    """The configured judge, or ``None`` when no API key is set (rules-only extraction)."""
    key = settings.judge_api_key.get_secret_value() if settings.judge_api_key else ""
    if settings.judge_provider == "openai_compat":
        if not (settings.judge_base_url and settings.judge_model):
            return None
        from constraintlens.adapters.judge.openai_compat import OpenAICompatJudge

        return OpenAICompatJudge(
            base_url=settings.judge_base_url, model=settings.judge_model, api_key=key or None,
            prompt_version=settings.judge_prompt_version,
            prompt_dir=settings.judge_prompt_dir or None, timeout_s=settings.judge_timeout_s,
        )  # fmt: skip
    if not key:
        return None
    from constraintlens.adapters.judge.anthropic import DEFAULT_MODEL, AnthropicJudge

    return AnthropicJudge(
        api_key=key, model=settings.judge_model or DEFAULT_MODEL,
        prompt_version=settings.judge_prompt_version,
        prompt_dir=settings.judge_prompt_dir or None, timeout_s=settings.judge_timeout_s,
        effort=settings.judge_effort or None, fallbacks=settings.judge_fallbacks,
    )  # fmt: skip


def models_from(settings: Settings) -> tuple[NLIModel | None, Embedder | None]:
    """Local models from ``CL_NLI_MODEL`` / ``CL_EMBED_MODEL``. A model that cannot load is
    logged and left out: its tier is then unavailable and health reports it."""
    nli: NLIModel | None = None
    embedder: Embedder | None = None
    if settings.enable_tier_1c and settings.nli_model:
        try:
            from constraintlens.adapters.models.nli import CrossEncoderNLI

            nli = CrossEncoderNLI(settings.nli_model)
        except Exception:
            log.exception("NLI model %s could not be loaded; Tier 1c disabled", settings.nli_model)
    if settings.embed_model:
        try:
            from constraintlens.adapters.models.sentence_transformers import (
                SentenceTransformerEmbedder,
            )

            embedder = SentenceTransformerEmbedder(settings.embed_model)
        except Exception:
            log.exception("embedder %s could not be loaded; routing off", settings.embed_model)
    return nli, embedder


async def default_container(settings: Settings) -> Container:
    store = await SQLiteEventStore(settings.db_url).initialize()
    nli, embedder = models_from(settings)
    return Container.build(
        settings, store=store, judge=judge_from(settings), nli=nli, embedder=embedder,
        judge_cache=store.judge_cache() if settings.judge_cache else None,
    )  # fmt: skip
