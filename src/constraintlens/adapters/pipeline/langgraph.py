"""LangGraph PipelineAdapter (ARCHITECTURE.md §7.3).

LangGraph passes a state dict between nodes. The envelope rides in that state under a
reserved key, next to (never inside) the natural-language task text, so agents never
parse it (§7.2). No LangGraph import is needed: the adapter works on the state mapping.
"""

from __future__ import annotations

from typing import Any

from constraintlens.domain.models import Envelope

ENVELOPE_KEY = "cl_envelope"


class LangGraphStateAdapter:
    """Reads/writes the envelope and task text in a LangGraph state dict."""

    def __init__(self, text_key: str = "text", envelope_key: str = ENVELOPE_KEY) -> None:
        self.text_key = text_key
        self.envelope_key = envelope_key

    def read_envelope(self, message: dict[str, Any]) -> Envelope | None:
        raw = message.get(self.envelope_key)
        if raw is None:
            return None
        return raw if isinstance(raw, Envelope) else Envelope.model_validate(raw)

    def write_envelope(self, message: dict[str, Any], envelope: Envelope) -> dict[str, Any]:
        return {**message, self.envelope_key: envelope.model_dump(mode="json")}

    def read_text(self, message: dict[str, Any]) -> str:
        return str(message.get(self.text_key, ""))

    def write_text(self, message: dict[str, Any], text: str) -> dict[str, Any]:
        return {**message, self.text_key: text}
