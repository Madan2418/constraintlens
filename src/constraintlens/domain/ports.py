"""Ports: the only interfaces the domain core depends on (ARCHITECTURE.md §6.3).

Each port is a small ``typing.Protocol`` (interface segregation, §5). Adapters in
``constraintlens.adapters`` implement them; every adapter, real or fake, must pass
the same contract suite in ``tests/contract``.

I/O-bound ports (Judge, EventStore, QueueTransport) are async. CPU-bound or
in-memory ports (Embedder, NLIModel, PipelineAdapter, Clock) are sync; the
application layer offloads CPU-heavy calls to a worker thread. See ADR 0002.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from constraintlens.domain.models import (
        Delivery,
        Envelope,
        Event,
        EventDraft,
        JudgeResult,
        ProposedAction,
    )

MessageT = TypeVar("MessageT")


@runtime_checkable
class Judge(Protocol):
    """Tier 2 entailment judge (§7.4).

    Returns ENTAILED / CONTRADICTED / AMBIGUOUS with a one-line reason. Raises
    ``DependencyUnavailable`` on timeout, transport error, or output that still
    fails schema validation after one retry.
    """

    @property
    def model_id(self) -> str:
        """Identifier of the judging model; part of the cache key."""
        ...

    @property
    def prompt_version(self) -> str:
        """Version of the judge prompt (e.g. ``"v1"``); part of the cache key."""
        ...

    async def judge(
        self,
        *,
        canonical_text: str,
        task_text: str,
        proposed_action: ProposedAction | None = None,
    ) -> JudgeResult: ...

    async def extract_constraints(
        self,
        *,
        instruction: str,
        issued_at: datetime,
        repair_hint: str | None = None,
    ) -> str:
        """Structured-output extraction call used by the LLM extractor (§7.1).

        Returns the model's raw output, which should be a JSON object
        ``{"constraints": [...]}`` matching ``docs/judge_prompts/extract_v1.md``. The
        domain validates it; on invalid output it calls again once with
        ``repair_hint`` set to the validation error.
        """
        ...


@runtime_checkable
class EventStore(Protocol):
    """Append-only event log (§12.1).

    Contract: ``append`` is atomic per call, assigns strictly increasing
    ``event_id`` values starting at 1, and never modifies stored events.
    """

    async def append(self, drafts: Sequence[EventDraft]) -> list[Event]: ...

    async def read_trace(self, trace_id: UUID) -> list[Event]:
        """All events for one trace, in ``event_id`` order."""
        ...

    async def read_after(
        self,
        after_event_id: int = 0,
        *,
        trace_id: UUID | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        """Events with ``event_id > after_event_id`` in order (stream by offset, §11.3)."""
        ...


@runtime_checkable
class QueueTransport(Protocol):
    """At-least-once envelope transport with consumer groups (§9.1).

    Modeled on Redis Streams: ``consume`` delivers new messages, ``reclaim``
    re-delivers messages left pending by a consumer that did not ack, and every
    re-delivery increments ``delivery_count``.
    """

    async def ensure_group(self, stream: str, group: str) -> None:
        """Create the consumer group if missing (idempotent)."""
        ...

    async def publish(self, stream: str, envelope: Envelope) -> str:
        """Append an envelope; returns the transport message id."""
        ...

    async def consume(
        self, stream: str, group: str, consumer: str, *, count: int = 1
    ) -> list[Delivery]:
        """Deliver up to ``count`` messages never delivered to this group."""
        ...

    async def reclaim(
        self, stream: str, group: str, consumer: str, *, min_idle_ms: int, count: int = 1
    ) -> list[Delivery]:
        """Re-deliver pending (unacked) messages idle for at least ``min_idle_ms``."""
        ...

    async def ack(self, stream: str, group: str, message_id: str) -> None: ...


@runtime_checkable
class PipelineAdapter(Protocol[MessageT]):
    """Reads/writes the envelope inside a framework's native message or state (§7.3)."""

    def read_envelope(self, message: MessageT) -> Envelope | None:
        """The envelope carried by ``message``, or ``None`` if it was stripped."""
        ...

    def write_envelope(self, message: MessageT, envelope: Envelope) -> MessageT:
        """A message carrying ``envelope``; the input message is not mutated."""
        ...


@runtime_checkable
class JudgeCache(Protocol):
    """Tier 2 verdict cache (§7.4), keyed by
    ``hash(canonical_text || normalized(T_k) || prompt_version || judge_model)``."""

    async def get(self, key: str) -> JudgeResult | None: ...

    async def put(
        self, key: str, result: JudgeResult, *, model: str, prompt_version: str
    ) -> None: ...


@runtime_checkable
class Embedder(Protocol):
    """Text encoder used for routing and cache keys only, never for ENTAILED (§7.4)."""

    @property
    def dimension(self) -> int: ...

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """One L2-normalised vector of length ``dimension`` per input text."""
        ...


@runtime_checkable
class NLIModel(Protocol):
    """Tier 1c natural-language-inference scorer (§7.4, §17 ``tier1c_nli.py``)."""

    def entailment(self, premise: str, hypothesis: str) -> float:
        """Probability in [0, 1] that ``premise`` entails ``hypothesis``."""
        ...


@runtime_checkable
class Clock(Protocol):
    """Time source; injected so async semantics are testable (§6.3)."""

    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds, for latency measurement."""
        ...


__all__ = [
    "Clock",
    "Embedder",
    "EventStore",
    "Judge",
    "JudgeCache",
    "MessageT",
    "NLIModel",
    "PipelineAdapter",
    "QueueTransport",
]
