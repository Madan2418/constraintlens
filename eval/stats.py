"""Bootstrap confidence intervals (ARCHITECTURE.md §14.4 step 4, §14.5).

Chains are the resampling unit: a chain's checkpoints are not independent of each
other, so resampling verdicts would understate the uncertainty. The percentile
bootstrap is used with a fixed seed, so every interval in a report is reproducible.
"""

from __future__ import annotations

from random import Random
from typing import TYPE_CHECKING

from eval.metrics import headline

if TYPE_CHECKING:
    from collections.abc import Sequence

    from eval.metrics import Outcome

DEFAULT_RESAMPLES = 1000
DEFAULT_SEED = 7


def bootstrap(
    outs: Sequence[Outcome],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    level: float = 0.95,
) -> dict[str, tuple[float, float] | None]:
    """Percentile intervals for every headline metric."""
    if not outs:
        return {}
    rng = Random(seed)
    n = len(outs)
    samples: dict[str, list[float]] = {}
    for _ in range(resamples):
        draw = [outs[rng.randrange(n)] for _ in range(n)]
        for k, v in headline(draw).items():
            if v is not None:
                samples.setdefault(k, []).append(v)
    lo_q, hi_q = (1 - level) / 2, 1 - (1 - level) / 2
    out: dict[str, tuple[float, float] | None] = {}
    for k in headline(outs):
        vs = sorted(samples.get(k, []))
        if len(vs) < resamples // 2:  # undefined in too many resamples
            out[k] = None
            continue
        lo = vs[min(len(vs) - 1, int(lo_q * len(vs)))]
        hi = vs[min(len(vs) - 1, int(hi_q * len(vs)))]
        out[k] = (round(lo, 4), round(hi, 4))
    return out
