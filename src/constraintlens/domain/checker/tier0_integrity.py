"""Tier 0: deterministic integrity (ARCHITECTURE.md §7.2, §7.4, §9.5).

Microsecond checks that need no semantics, against the service's registered set:

* constraint lost (envelope carries none) or dropped (some registered id missing);
* hash mismatch: an envelope constraint differs from the registered one, or its own
  content no longer matches its hash, or the envelope digest is wrong;
* expired: ``now > expiry`` for a registered constraint.

Any of these failures is terminal (§7.4 rule 1). Tier 0 also reports the async
guard's findings at CONSUME (``OUT_OF_ORDER``, ``MISSING_HOP``; §7.4 "hop_seq valid",
§9.4). Staleness is enforced by the checker after the semantic tiers (§9.3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from constraintlens.domain.canonical import compute_envelope_digest, verify_content_hash
from constraintlens.domain.checker.pipeline import Finding, TierOutcome
from constraintlens.domain.composition import find_dropped
from constraintlens.domain.models import Failure, TierId

if TYPE_CHECKING:
    from constraintlens.domain.checker.pipeline import CheckContext


class IntegrityTier:
    name: TierId = "T0"

    def applies(self, ctx: CheckContext) -> bool:
        return True

    async def run(self, ctx: CheckContext) -> TierOutcome:
        env = ctx.request.envelope
        registered = ctx.registered_by_id()
        findings = [Finding(f) for f in find_dropped(ctx.registered, env.constraints)]

        for c in env.constraints:
            reg = registered.get(c.constraint_id)
            if reg is None:
                continue  # unregistered extras carry no authority (§7.2)
            if c.content_hash != reg.content_hash or c != reg:
                findings.append(
                    Finding(
                        Failure(
                            kind="HASH_MISMATCH",
                            constraint_id=c.constraint_id,
                            detail="envelope copy differs from the registered constraint",
                        )
                    )
                )
            elif not verify_content_hash(c):
                findings.append(
                    Finding(
                        Failure(
                            kind="HASH_MISMATCH",
                            constraint_id=c.constraint_id,
                            detail="constraint content does not match its content_hash",
                        )
                    )
                )

        if env.constraints and env.envelope_digest != compute_envelope_digest(
            env.trace_id, env.constraints
        ):
            findings.append(
                Finding(
                    Failure(
                        kind="HASH_MISMATCH",
                        constraint_id=None,
                        detail="envelope_digest does not match the constraints it carries",
                    )
                )
            )

        for reg in ctx.registered:
            if reg.expiry is not None and ctx.now > reg.expiry:
                findings.append(
                    Finding(
                        Failure(
                            kind="EXPIRED",
                            constraint_id=reg.constraint_id,
                            detail=f"expired at {reg.expiry.isoformat()}",
                        )
                    )
                )
        if findings:  # integrity failures are terminal (§7.4 rule 1)
            return TierOutcome(findings=findings, terminal=True)

        # Async guard (§9.4). A held gap is terminal: nothing else can be decided before
        # the predecessor arrives. OUT_OF_ORDER and a released gap are flagged, and the
        # checkpoint is still checked by the later tiers ("checked, not trusted").
        guard = ctx.assessment
        async_findings = [
            Finding(f, held=guard.held and f.kind == "MISSING_HOP") for f in guard.failures
        ]
        return TierOutcome(findings=async_findings, terminal=guard.held)
