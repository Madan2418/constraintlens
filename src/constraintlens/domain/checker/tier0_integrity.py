"""Tier 0: deterministic integrity (ARCHITECTURE.md §7.2, §7.4, §9.5).

Microsecond checks that need no semantics, against the service's registered set:

* constraint lost (envelope carries none) or dropped (some registered id missing);
* hash mismatch: an envelope constraint differs from the registered one, or its own
  content no longer matches its hash, or the envelope digest is wrong;
* expired: ``now > expiry`` for a registered constraint.

Any failure is terminal (§7.4 rule 1). Ordering (OUT_OF_ORDER / MISSING_HOP) and
staleness belong to the async guard (Phase 4).
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
        return TierOutcome(findings=findings, terminal=bool(findings))
