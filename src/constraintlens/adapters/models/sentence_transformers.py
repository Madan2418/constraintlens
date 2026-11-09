"""sentence-transformers Embedder (ARCHITECTURE.md §6.3, §7.4).

Used for routing only (Tier 1c's "clearly irrelevant" short-circuit), never to decide
ENTAILED. Optional dependency group ``models``; imported lazily.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str, *, device: str | None = None, model: Any = None) -> None:
        if model is None:
            from sentence_transformers import SentenceTransformer  # lazy: optional dependency

            model = SentenceTransformer(model_name, device=device)
        self._model = model
        self.model_name = model_name

    @property
    def dimension(self) -> int:
        return int(self._model.get_sentence_embedding_dimension())

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.encode(list(texts), normalize_embeddings=True)
        return [[float(x) for x in v] for v in vectors]
