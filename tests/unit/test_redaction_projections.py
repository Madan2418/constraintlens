from __future__ import annotations

from uuid import uuid4

import pytest

from constraintlens.domain.projections import base_hop_id, fold, retry_hop_id
from constraintlens.domain.redaction import redact
from tests.factories import make_draft


class TestRedaction:
    def test_masks_each_kind(self) -> None:
        text = "Mail ana@example.co.uk, card 4111 1111 1111 1111, call +1 (415) 555-0100."
        assert redact(text) == "Mail [EMAIL], card [CARD], call [PHONE]."

    def test_selected_kinds_only_and_ids_untouched(self) -> None:
        assert redact("ana@x.io 555 123 4567", ["email"]) == "[EMAIL] 555 123 4567"
        assert redact("Refund order #4471, limit $100.") == "Refund order #4471, limit $100."

    def test_unknown_pattern(self) -> None:
        with pytest.raises(ValueError, match="unknown redaction"):
            redact("x", ["ssn"])


class TestProjection:
    def test_status_lifecycle_and_refusal(self) -> None:
        t = uuid4()
        drafts = [make_draft(t, type_="TRACE_OPENED"), make_draft(t, type_="TRACE_CLOSED")]
        events = [d.with_id(i + 1) for i, d in enumerate(drafts)]
        assert fold(t, events[:1]).status == "open"
        assert fold(t, events[:1]).outcome == "OPEN"
        closed = fold(t, events)
        assert closed.status == "closed"
        assert closed.outcome == "ALLOWED"
        assert closed.last_event_id == 2
        refused = make_draft(t, type_="WARNING").model_copy(update={"payload": {"refused": True}})
        assert fold(t, [refused.with_id(1)]).status == "refused"
        other = make_draft(uuid4(), type_="TRACE_OPENED").with_id(1)
        assert fold(t, [other]).status == "unknown"

    def test_retry_hop_ids(self) -> None:
        assert retry_hop_id("h2", 1) == "h2/r1"
        assert retry_hop_id("h2/r1", 2) == "h2/r2"
        assert base_hop_id("h2/r2") == "h2"
        assert base_hop_id("h2") == "h2"
        with pytest.raises(ValueError, match="start at 1"):
            retry_hop_id("h2", 0)
