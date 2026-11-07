"""``StubAdapter``: PipelineAdapter fake over plain dict messages (§6.3).

The envelope travels as JSON under a reserved key, as it would in a framework's
serialised state, so a round trip exercises real serialisation.
"""

from __future__ import annotations

from typing import Any

from constraintlens.domain.models import Envelope

ENVELOPE_KEY = "__constraintlens_envelope__"


class StubAdapter:
    def read_envelope(self, message: dict[str, Any]) -> Envelope | None:
        raw = message.get(ENVELOPE_KEY)
        return None if raw is None else Envelope.model_validate_json(raw)

    def write_envelope(self, message: dict[str, Any], envelope: Envelope) -> dict[str, Any]:
        return {**message, ENVELOPE_KEY: envelope.model_dump_json()}
