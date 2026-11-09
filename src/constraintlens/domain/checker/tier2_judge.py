"""Tier 2: LLM judge with cache (ARCHITECTURE.md §7.4, §10.1, §10.3, §21.1).

Decides the constraints the earlier tiers could not: those escalated from the NLI band
(or from an unavailable NLI model), and stale constraints no semantic tier has
re-verified yet (§9.3). For each one it asks the ``Judge`` port whether T_k entails
``canonical_text(C)``:

* ENTAILED → a passing score (the judge's confidence);
* CONTRADICTED → ``TEXTUAL_DRIFT``;
* AMBIGUOUS → ``TEXTUAL_DRIFT`` marked ambiguous: the risk class decides (§7.4 rule 5).

**Manipulation guard (§21.1).** An ENTAILED verdict must quote the span of the task
text it relied on (``evidence``), and that span must really occur in T_k. Otherwise it
is downgraded to AMBIGUOUS. Task text crafted to coax "ENTAILED" therefore has to
contain words that express the constraint.

**Cache.** Key = ``sha256(canonical_text || normalized(T_k) || prompt_version ||
judge_model)``. Only well-formed results are stored. ``use_cache=False`` turns it off
for evaluation runs where independence matters, and ``call_judge=False`` makes the tier
cache-only (replay must not spend money or write anything, §11.1).

**Failure.** A judge that raises, times out (10 s hard limit, §10.3) or is unavailable
makes the verdict degraded for that constraint, and the fail policy decides (§10.1).
Every failure is reported for a ``JUDGE_FAILED`` event.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from typing import TYPE_CHECKING

from constraintlens.domain.checker.pipeline import Finding, TierOutcome
from constraintlens.domain.checker.tier1b_lexical import TEXT_CHECKPOINTS
from constraintlens.domain.models import Failure, JudgeResult, TierId

if TYPE_CHECKING:
    from uuid import UUID

    from constraintlens.domain.checker.pipeline import CheckContext
    from constraintlens.domain.models import Constraint
    from constraintlens.domain.ports import Judge, JudgeCache

_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _SPACE.sub(" ", text).strip().casefold()


def judge_cache_key(canonical_text: str, task_text: str, prompt_version: str, model: str) -> str:
    """§7.4: ``hash(canonical_text(C) || normalized(T_k) || prompt_version || judge_model)``."""
    blob = "\x1f".join([canonical_text, normalize(task_text), prompt_version, model])
    return hashlib.sha256(blob.encode()).hexdigest()


def evidence_supported(result: JudgeResult, task_text: str) -> bool:
    ev = normalize(result.evidence or "").strip("\"'“”‘’ ")
    return len(ev) >= 3 and ev in normalize(task_text)


class JudgeTier:
    name: TierId = "T2"
    semantic = True

    def __init__(
        self,
        judge: Judge,
        *,
        cache: JudgeCache | None = None,
        timeout_s: float = 10.0,
        use_cache: bool = True,
        call_judge: bool = True,
    ) -> None:
        self.judge = judge
        self.cache = cache if use_cache else None
        self.timeout_s = timeout_s
        self.call_judge = call_judge

    def applies(self, ctx: CheckContext) -> bool:
        return (
            ctx.request.checkpoint in TEXT_CHECKPOINTS
            and bool(ctx.request.envelope.payload_text.strip())
            and bool(ctx.needs_judgement())
        )

    async def _one(self, c: Constraint, text: str) -> tuple[JudgeResult | None, bool, str | None]:
        """``(result, cache_hit, failure)`` for one constraint."""
        key = judge_cache_key(
            c.canonical_text, text, self.judge.prompt_version, self.judge.model_id
        )
        if self.cache is not None:
            try:
                cached = await self.cache.get(key)
            except Exception:  # a broken cache must not break verification
                cached = None
            if cached is not None:
                return cached, True, None
        if not self.call_judge:
            return None, False, "judge calls disabled (cache-only mode) and no cached verdict"
        try:
            result = await asyncio.wait_for(
                self.judge.judge(canonical_text=c.canonical_text, task_text=text),
                self.timeout_s,
            )
        except TimeoutError:
            return None, False, f"judge timed out after {self.timeout_s:.1f}s"
        except Exception as exc:
            return None, False, f"judge failed: {exc.__class__.__name__}: {exc}"[:500]
        if self.cache is not None:
            with contextlib.suppress(Exception):  # a failed cache write is not fatal
                await self.cache.put(
                    key, result, model=self.judge.model_id,
                    prompt_version=self.judge.prompt_version,
                )  # fmt: skip
        return result, False, None

    async def run(self, ctx: CheckContext) -> TierOutcome:
        text = ctx.request.envelope.payload_text
        targets = ctx.needs_judgement()
        answers = await asyncio.gather(*(self._one(c, text) for c in targets))
        findings: list[Finding] = []
        scores: dict[UUID, float] = {}
        degraded_ids: list[UUID] = []
        failures: list[str] = []
        notes: list[str] = []
        hit = False
        for c, (result, cache_hit, failure) in zip(targets, answers, strict=True):
            cid = c.constraint_id
            hit |= cache_hit
            if result is None:
                degraded_ids.append(cid)
                failures.append(f"{cid}: {failure}")
                continue
            if result.verdict == "ENTAILED" and not evidence_supported(result, text):
                notes.append(f"constraint {cid}: ENTAILED without verifiable evidence; ambiguous")
                result = result.model_copy(
                    update={"verdict": "AMBIGUOUS", "reason": f"unverified: {result.reason}"}
                )
            if result.verdict == "ENTAILED":
                scores[cid] = result.confidence
            elif result.verdict == "CONTRADICTED":
                scores[cid] = round(1.0 - result.confidence, 4)
                findings.append(
                    Finding(
                        Failure(kind="TEXTUAL_DRIFT", constraint_id=cid,
                                detail=f"judge: contradicted ({result.reason})"[:500])
                    )
                )  # fmt: skip
            else:
                scores[cid] = 0.5
                findings.append(
                    Finding(
                        Failure(kind="TEXTUAL_DRIFT", constraint_id=cid,
                                detail=f"judge: ambiguous ({result.reason})"[:500]),
                        ambiguous=True,
                    )
                )  # fmt: skip
        return TierOutcome(
            findings=findings,
            terminal=True,
            scores=scores,
            degraded=bool(degraded_ids),
            degraded_constraint_ids=degraded_ids,
            cache_hit=hit,
            resolved=[c.constraint_id for c in targets],
            judge_failures=failures,
            notes=notes,
        )
