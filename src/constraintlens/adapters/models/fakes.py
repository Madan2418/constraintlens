"""Deterministic model fakes: ``HashEmbedder`` (Embedder) and ``ScriptedNLI`` (NLIModel).

``HashEmbedder`` is a hashed bag-of-words encoder. It is stable across processes
(uses blake2b, not ``hash()``), so test results never depend on PYTHONHASHSEED.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_TOKEN = re.compile(r"[a-z0-9$.]+")


class HashEmbedder:
    def __init__(self, dimension: int = 64) -> None:
        if dimension < 1:
            raise ValueError("dimension must be positive")
        self._dim = dimension

    @property
    def dimension(self) -> int:
        return self._dim

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._encode_one(t) for t in texts]

    def _encode_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in _TOKEN.findall(text.lower()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "big") % self._dim
            vec[idx] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            # Empty or token-less text: a fixed unit vector keeps the output normalised.
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]


class ScriptedNLI:
    """Returns scripted entailment scores by ``(premise, hypothesis)``; default otherwise."""

    def __init__(
        self,
        scores: Mapping[tuple[str, str], float] | None = None,
        *,
        default: float = 0.5,
        fail_with: Exception | None = None,
    ) -> None:
        for s in [default, *(scores or {}).values()]:
            if not 0.0 <= s <= 1.0:
                raise ValueError(f"score {s} outside [0, 1]")
        self._scores = dict(scores or {})
        self._default = default
        self.fail_with = fail_with

    def entailment(self, premise: str, hypothesis: str) -> float:
        if self.fail_with is not None:
            raise self.fail_with
        return self._scores.get((premise, hypothesis), self._default)
