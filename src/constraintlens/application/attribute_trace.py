"""``AttributeTrace`` use case (ARCHITECTURE.md §7.7, §11.1, §12.1, ADR 0009).

Attribution is a view over the event log. ``AttributeTrace`` reads a trace's events
and returns the culprit per failed constraint. It writes nothing, so ``GET
/v1/traces/{id}/attribution`` can recompute it with other parameters at any time
("computed lazily on failure for live traces", §7.7).

``record`` is the one write: ``CloseTrace`` calls it to append the final attribution
as an ``ATTRIBUTION_COMPUTED`` event, in the same batch as ``TRACE_CLOSED``, so the
audit trail keeps what was concluded and under which parameters.

Replayed attribution (``outcomes``) runs the same algorithm over the verdicts a replay
recomputed, which is how attribution is evaluated under new thresholds or tier
ablations (§14.4) without touching the stored trace.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from constraintlens.domain.attribution import AttributionParams, attribute_events
from constraintlens.domain.errors import UnknownTrace
from constraintlens.domain.models import EventDraft
from constraintlens.domain.projections import fold

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from constraintlens.domain.attribution import CheckOutcome
    from constraintlens.domain.models import Attribution
    from constraintlens.domain.ports import Clock, EventStore


@dataclass(frozen=True)
class AttributionReport:
    trace_id: UUID
    params: AttributionParams
    attributions: list[Attribution]
    through_event_id: int  # the log position the view was computed at

    def as_payload(self) -> dict[str, Any]:
        return {
            "attributions": [a.model_dump(mode="json") for a in self.attributions],
            "params": self.params.as_dict(),
            "through_event_id": self.through_event_id,
        }


class AttributeTrace:
    def __init__(
        self, *, store: EventStore, clock: Clock, params: AttributionParams | None = None
    ) -> None:
        self._store = store
        self._clock = clock
        self.params = params or AttributionParams()

    def with_overrides(
        self, *, theta: float | None = None, delta_min: float | None = None
    ) -> AttributionParams:
        """The default parameters with any given ones replaced (validated)."""
        changes: dict[str, float] = {}
        if theta is not None:
            changes["theta"] = theta
        if delta_min is not None:
            changes["delta_min"] = delta_min
        return replace(self.params, **changes)  # type: ignore[arg-type]

    async def __call__(
        self,
        trace_id: UUID,
        *,
        params: AttributionParams | None = None,
        outcomes: Mapping[int, CheckOutcome] | None = None,
    ) -> AttributionReport:
        events = await self._store.read_trace(trace_id)
        state = fold(trace_id, events)
        if state.status == "unknown":
            raise UnknownTrace(f"trace {trace_id} was never opened")
        params = params or self.params
        return AttributionReport(
            trace_id=trace_id,
            params=params,
            attributions=attribute_events(trace_id, events, params, outcomes=outcomes),
            through_event_id=state.last_event_id,
        )

    async def draft(self, trace_id: UUID) -> EventDraft:
        """The ``ATTRIBUTION_COMPUTED`` event for the trace as it stands (not appended)."""
        report = await self(trace_id)
        return EventDraft(
            trace_id=trace_id,
            type="ATTRIBUTION_COMPUTED",
            occurred_at=self._clock.now(),
            payload=report.as_payload(),
        )
