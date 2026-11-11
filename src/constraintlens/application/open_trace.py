"""``OpenTrace`` use case (ARCHITECTURE.md §6.2, §7.1, §7.6, §11.1 ``POST /v1/traces``).

1. Extract constraints from the user's instruction (rules, LLM fallback, reconcile).
2. Add declared org / system policy constraints (they bypass extraction, §7.1).
3. Issue each one: id, issue time, canonical text, content hash.
4. Compose. Refuse the trace if the set is ``UNSATISFIABLE`` (§7.6).
5. Record ``TRACE_OPENED`` and one ``CONSTRAINT_REGISTERED`` per constraint, then
   return the root envelope (hop 0, T_0 = the instruction).

A refusal is a decision, so it is recorded too (a ``WARNING`` event) before
``UnsatisfiableConstraints`` is raised (§7.9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from constraintlens.domain.canonical import compute_envelope_digest, issue, sha256_hex
from constraintlens.domain.composition import Composition, compose
from constraintlens.domain.errors import UnsatisfiableConstraints
from constraintlens.domain.extraction import (
    ExtractionContext,
    ExtractionResult,
    LLMMode,
    extract,
)
from constraintlens.domain.models import (
    Constraint,
    ConstraintSpec,
    Envelope,
    EventDraft,
    HopMeta,
    RiskClass,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from constraintlens.domain.ports import Clock, EventStore, Judge

ROOT_HOP_ID = "root"


@dataclass(frozen=True)
class OpenTraceRequest:
    instruction: str
    owner: str  # the issuing principal, e.g. "user:alice"
    declared: list[ConstraintSpec] = field(default_factory=list)  # org / system policies
    risk_class: RiskClass = "high"
    #: False: register only ``declared`` (callers that already hold structured
    #: constraints, e.g. benchmark replay; ADR 0009). The instruction is still T_0.
    extract: bool = True


@dataclass(frozen=True)
class OpenTraceResult:
    trace_id: UUID
    constraints: list[Constraint]
    envelope: Envelope
    composition: Composition
    extraction: ExtractionResult


class OpenTrace:
    def __init__(
        self,
        *,
        store: EventStore,
        clock: Clock,
        judge: Judge | None = None,
        llm_mode: LLMMode = "fallback",
        default_max_age_s: int | None = 3600,
        store_full_text: bool = False,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._store = store
        self._clock = clock
        self._judge = judge
        self._llm_mode = llm_mode
        self._default_max_age_s = default_max_age_s
        self._store_full_text = store_full_text
        self._new_id = id_factory

    async def __call__(self, request: OpenTraceRequest) -> OpenTraceResult:
        issued_at = self._clock.now()
        trace_id = self._new_id()
        context = ExtractionContext(
            owner=request.owner,
            issued_at=issued_at,
            risk_class=request.risk_class,
            max_age_seconds=self._default_max_age_s,
        )
        extraction = (
            await extract(request.instruction, context, judge=self._judge, llm_mode=self._llm_mode)
            if request.extract
            else ExtractionResult(specs=[], sources=[])
        )

        specs = [*extraction.specs, *(self._with_default_age(s) for s in request.declared)]
        constraints = [
            issue(spec, constraint_id=self._new_id(), issued_at=issued_at) for spec in specs
        ]
        composition = compose(constraints)

        instruction_record: dict[str, object] = {
            "instruction_sha256": sha256_hex(request.instruction),
            "instruction_chars": len(request.instruction),
        }
        if self._store_full_text:
            instruction_record["instruction"] = request.instruction

        if composition.unsatisfiable:
            details = [c.detail for c in composition.unsatisfiable]
            await self._store.append(
                [
                    EventDraft(
                        trace_id=trace_id,
                        type="WARNING",
                        occurred_at=issued_at,
                        payload={
                            "reason": "UNSATISFIABLE",
                            "refused": True,
                            "details": details,
                            **instruction_record,
                        },
                    )
                ]
            )
            raise UnsatisfiableConstraints("; ".join(details))

        envelope = Envelope(
            trace_id=trace_id,
            hop=HopMeta(
                hop_id=ROOT_HOP_ID,
                parent_hop_ids=[],
                hop_seq=0,
                agent_id=request.owner,
                emitted_at=issued_at,
            ),
            constraints=constraints,
            envelope_digest=compute_envelope_digest(trace_id, constraints),
            payload_text=request.instruction,
        )

        opened = EventDraft(
            trace_id=trace_id,
            type="TRACE_OPENED",
            hop_id=ROOT_HOP_ID,
            occurred_at=issued_at,
            payload={
                **instruction_record,
                "owner": request.owner,
                "constraint_ids": [str(c.constraint_id) for c in constraints],
                "envelope_digest": envelope.envelope_digest,
                "extraction": {
                    "sources": extraction.sources,
                    "flags": [f.model_dump(mode="json") for f in extraction.flags],
                    "llm_called": extraction.llm_called,
                    "degraded": extraction.degraded,
                },
                "conflicts": [c.model_dump(mode="json") for c in composition.conflicts],
            },
        )
        registered = [
            EventDraft(
                trace_id=trace_id,
                type="CONSTRAINT_REGISTERED",
                hop_id=ROOT_HOP_ID,
                occurred_at=issued_at,
                payload={"constraint": c.model_dump(mode="json")},
            )
            for c in constraints
        ]
        await self._store.append([opened, *registered])
        return OpenTraceResult(
            trace_id=trace_id,
            constraints=constraints,
            envelope=envelope,
            composition=composition,
            extraction=extraction,
        )

    def _with_default_age(self, spec: ConstraintSpec) -> ConstraintSpec:
        if spec.max_age_seconds is not None or self._default_max_age_s is None:
            return spec
        return spec.model_copy(update={"max_age_seconds": self._default_max_age_s})
