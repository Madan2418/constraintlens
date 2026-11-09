"""Cross-encoder NLI adapter for Tier 1c (ARCHITECTURE.md §7.4, §19.4).

Wraps a ``sentence_transformers.CrossEncoder`` NLI model (``CL_NLI_MODEL``, e.g. a
DeBERTa or MiniLM cross-encoder trained on MNLI/SNLI). The model's logits are softmaxed
and the probability of its entailment label is returned. The label index is read from
the model config, never assumed, because label order differs between checkpoints.

Optional dependency group ``models`` (``uv sync --group models``); imported lazily.
Runs on CPU unless ``device`` says otherwise.
"""

from __future__ import annotations

from typing import Any


class CrossEncoderNLI:
    def __init__(self, model_name: str, *, device: str | None = None, model: Any = None) -> None:
        if model is None:
            from sentence_transformers import CrossEncoder  # lazy: optional dependency

            model = CrossEncoder(model_name, device=device)
        self._model = model
        id2label: dict[int, str] = dict(model.model.config.id2label)
        matches = [int(i) for i, label in id2label.items() if "entail" in str(label).lower()]
        if len(matches) != 1:
            raise ValueError(f"{model_name}: cannot find a unique entailment label in {id2label}")
        self._entail = matches[0]
        self.model_name = model_name

    def entailment(self, premise: str, hypothesis: str) -> float:
        probs = self._model.predict([(premise, hypothesis)], apply_softmax=True)
        return float(min(1.0, max(0.0, probs[0][self._entail])))
