"""Tier 1c: NLI cross-encoder (ARCHITECTURE.md §7.4, §10.1, §10.3).

For each registered constraint, a local natural-language-inference model scores whether
the task text T_k (premise) entails ``canonical_text(C)`` (hypothesis):

* score ≥ θ_high → ENTAILED (a passing score; also re-verifies a stale constraint);
* score < θ_low  → NOT_ENTAILED → ``TEXTUAL_DRIFT``;
* in the band   → escalate to Tier 2 (§7.4 rule 3).

Embedding similarity, when an embedder is configured, only short-circuits the clearly
irrelevant case: a band score whose text is plainly about something else is decided
NOT_ENTAILED without a judge call (rule 4). It never produces ENTAILED.

The model is CPU-bound and synchronous (``NLIModel`` port, ADR 0002), so it runs in a
worker thread under the 500 ms hard timeout (§10.3). A failure or timeout escalates
every constraint to Tier 2; if no Tier 2 settles them, the verdict is degraded and the
fail policy decides (§10.1 "NLI unavailable").
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING

from constraintlens.domain.checker.pipeline import Finding, TierOutcome
from constraintlens.domain.checker.tier1b_lexical import TEXT_CHECKPOINTS
from constraintlens.domain.models import Failure, TierId

if TYPE_CHECKING:
    from collections.abc import Sequence

    from constraintlens.domain.checker.pipeline import CheckContext
    from constraintlens.domain.ports import Embedder, NLIModel


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class NLITier:
    name: TierId = "T1c"
    semantic = True

    def __init__(
        self,
        nli: NLIModel,
        *,
        theta_low: float = 0.35,
        theta_high: float = 0.80,
        timeout_s: float = 0.5,
        embedder: Embedder | None = None,
        irrelevant_below: float = 0.15,
    ) -> None:
        if not 0.0 <= theta_low <= theta_high <= 1.0:
            raise ValueError("need 0 <= theta_low <= theta_high <= 1")
        self.nli = nli
        self.theta_low, self.theta_high = theta_low, theta_high
        self.timeout_s = timeout_s
        self.embedder = embedder
        self.irrelevant_below = irrelevant_below

    def applies(self, ctx: CheckContext) -> bool:
        return (
            ctx.request.checkpoint in TEXT_CHECKPOINTS
            and bool(ctx.registered)
            and bool(ctx.request.envelope.payload_text.strip())
        )

    def _score_all(self, premise: str, hypotheses: list[str]) -> list[float]:
        return [self.nli.entailment(premise, h) for h in hypotheses]

    def _similarities(self, text: str, hypotheses: list[str]) -> list[float]:
        assert self.embedder is not None
        vectors = self.embedder.encode([text, *hypotheses])
        return [cosine(vectors[0], v) for v in vectors[1:]]

    async def run(self, ctx: CheckContext) -> TierOutcome:
        text = ctx.request.envelope.payload_text
        constraints = list(ctx.registered)
        ids = [c.constraint_id for c in constraints]
        hypotheses = [c.canonical_text for c in constraints]
        try:
            scores = await asyncio.wait_for(
                asyncio.to_thread(self._score_all, text, hypotheses), self.timeout_s
            )
        except Exception as exc:  # timeout or model failure: Tier 2 takes over (§10.1)
            why = "timed out" if isinstance(exc, TimeoutError) else exc.__class__.__name__
            return TierOutcome(
                escalate=ids, notes=[f"NLI unavailable ({why}); escalated to Tier 2"]
            )
        for s in scores:
            if not 0.0 <= s <= 1.0:
                return TierOutcome(
                    escalate=ids, notes=[f"NLI returned an invalid score {s}; escalated"]
                )

        band = [i for i, s in enumerate(scores) if self.theta_low <= s < self.theta_high]
        similarity: dict[int, float] = {}
        if band and self.embedder is not None:
            try:
                sims = self._similarities(text, [hypotheses[i] for i in band])
                similarity = dict(zip(band, sims, strict=True))
            except Exception:  # routing is an optimisation; without it, escalate
                similarity = {}

        findings: list[Finding] = []
        escalate = []
        for i, (cid, s) in enumerate(zip(ids, scores, strict=True)):
            if s >= self.theta_high:
                continue
            if s < self.theta_low:
                detail = f"NLI entailment {s:.2f} below θ_low {self.theta_low:.2f}"
            elif similarity.get(i, 1.0) < self.irrelevant_below:
                detail = (
                    f"NLI entailment {s:.2f} in the band and the text is unrelated "
                    f"(similarity {similarity[i]:.2f}); not entailed"
                )
            else:
                escalate.append(cid)
                continue
            findings.append(
                Finding(Failure(kind="TEXTUAL_DRIFT", constraint_id=cid, detail=detail))
            )
        return TierOutcome(
            findings=findings,
            terminal=bool(findings),
            scores={cid: round(s, 4) for cid, s in zip(ids, scores, strict=True)},
            escalate=escalate,
        )
