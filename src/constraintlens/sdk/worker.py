"""Redis-worker integration: ``GuardedWorker`` (ARCHITECTURE.md §7.3, §9).

A consumer-side wrapper around a Redis Streams consumer loop. It works on any
``QueueTransport`` (Redis Streams in production, ``InMemoryQueue`` in tests). For each
delivery it:

1. runs ``CONSUME`` with the delivery's transport metadata (delivery count, enqueue
   and consume times), so the service can judge dwell, staleness, ordering and gaps;
2. hands the task to the worker's handler inside a hop context, so guarded tools the
   handler calls go through the ``TOOL_CALL`` gate;
3. runs ``PUBLISH`` (naming the destination queue) before emitting downstream, and
   re-grounds the handler when asked (bounded, a new hop id per attempt, ADR 0005);
4. acknowledges the message once a final decision is reached.

At-least-once delivery (§9.1, §9.6):

* a **held** CONSUME (missing predecessor) is *not* acknowledged, so the transport
  redelivers it (``reclaim``) and it is checked again;
* a **redelivered** message gets the stored verdicts. A tool call it re-issues was
  already authorized, so the SDK does not execute it twice (``DuplicateToolCall``) and
  the message is acknowledged;
* a blocked message is acknowledged: the decision is final and recorded;
* when the **service cannot answer**, the SDK's local fail-policy verdict applies
  (§10.1). A local BLOCK is not a recorded decision, so the message is *not*
  acknowledged: it stays pending and is checked once the service is back.

The queue message *is* the envelope: the task text rides in ``payload_text`` beside the
constraints, never inside them (§7.2), so this adapter's ``read_envelope`` and
``write_envelope`` are the identity (``PipelineAdapter``, §6.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from constraintlens.adapters.clock import SystemClock
from constraintlens.domain.models import TransportMeta
from constraintlens.domain.projections import retry_hop_id
from constraintlens.sdk.client import (
    BLOCKING,
    ConstraintViolation,
    DuplicateToolCall,
    next_envelope,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from constraintlens.domain.models import Delivery, Envelope, Verdict
    from constraintlens.domain.ports import Clock, QueueTransport
    from constraintlens.sdk.client import ConstraintLens

Outcome = Literal["done", "held", "blocked", "duplicate", "unavailable"]


@dataclass(frozen=True)
class WorkerTask:
    """What the handler sees: the task text (plus any constraint reminder) and its hop."""

    text: str
    envelope: Envelope  # this hop's acting envelope
    delivery: Delivery

    @property
    def redelivered(self) -> bool:
        return self.delivery.delivery_count > 1


@dataclass(frozen=True)
class ProcessResult:
    message_id: str
    hop_id: str
    outcome: Outcome
    acked: bool
    verdict: Verdict | None = None  # the deciding verdict, when one decided
    published_id: str | None = None  # downstream message id, if the worker emitted one


class EnvelopeMessageAdapter:
    """``PipelineAdapter`` for queue messages that are envelopes."""

    def read_envelope(self, message: Envelope) -> Envelope | None:
        return message

    def write_envelope(self, message: Envelope, envelope: Envelope) -> Envelope:
        return envelope


class GuardedWorker:
    def __init__(
        self,
        cl: ConstraintLens,
        transport: QueueTransport,
        *,
        stream: str,
        group: str,
        consumer: str,
        agent_id: str,
        out_stream: str | None = None,
        hop_id: Callable[[Envelope], str] | None = None,
        clock: Clock | None = None,
        reclaim_idle_ms: int = 30_000,
    ) -> None:
        self.cl = cl
        self.transport = transport
        self.stream, self.group, self.consumer = stream, group, consumer
        self.agent_id = agent_id
        self.out_stream = out_stream
        self._hop_id = hop_id or (lambda env: f"h{env.hop.hop_seq + 1}")
        self._clock = clock or SystemClock()
        self.reclaim_idle_ms = reclaim_idle_ms
        self.adapter = EnvelopeMessageAdapter()

    async def start(self) -> None:
        await self.transport.ensure_group(self.stream, self.group)

    async def poll(
        self,
        handler: Callable[[WorkerTask], Awaitable[str | None]],
        *,
        count: int = 1,
        reclaim: bool = True,
    ) -> list[ProcessResult]:
        """Process messages left pending (crashed consumers, held checkpoints), then new ones.

        Pending messages are reclaimed *before* new ones are read, so a message this call
        reads is never also reclaimed by it.
        """
        out: list[ProcessResult] = []
        if reclaim:
            for d in await self.transport.reclaim(
                self.stream, self.group, self.consumer,
                min_idle_ms=self.reclaim_idle_ms, count=count,
            ):  # fmt: skip
                out.append(await self.process(d, handler))
        for d in await self.transport.consume(self.stream, self.group, self.consumer, count=count):
            out.append(await self.process(d, handler))
        return out

    async def process(
        self, delivery: Delivery, handler: Callable[[WorkerTask], Awaitable[str | None]]
    ) -> ProcessResult:
        incoming = self.adapter.read_envelope(delivery.envelope)
        assert incoming is not None  # the transport always carries an envelope
        base_id = self._hop_id(incoming)
        meta = delivery.transport_meta(consumed_at=self._clock.now())
        reply = await self.cl._submit_ex("CONSUME", incoming, base_id, transport_meta=meta)
        consumed = reply.verdict
        if consumed.held:  # leave it pending: redelivery re-checks it (§9.4)
            return ProcessResult(delivery.message_id, base_id, "held", False, consumed)
        if reply.local and consumed.decision in BLOCKING:  # service down: retry later
            return ProcessResult(delivery.message_id, base_id, "unavailable", False, consumed)
        if consumed.decision in BLOCKING:
            return await self._finish(delivery, base_id, "blocked", consumed)

        reminder = _reminder(consumed)
        attempt, current_id = 0, base_id
        while True:
            text_in = incoming.payload_text + reminder
            acting = self._envelope(incoming, current_id, text_in)
            try:
                async with self.cl.hop(acting):
                    out_text = await handler(WorkerTask(text_in, acting, delivery))
            except DuplicateToolCall as dup:
                return await self._finish(delivery, current_id, "duplicate", dup.verdict)
            except ConstraintViolation as blocked:
                return await self._finish(delivery, current_id, "blocked", blocked.verdict)
            if out_text is None or self.out_stream is None:
                return await self._finish(delivery, current_id, "done", None)

            emitted = self._envelope(incoming, current_id, out_text)
            sent = await self.cl._submit_ex(
                "PUBLISH", emitted, current_id,
                transport_meta=TransportMeta(destination=self.out_stream),
            )  # fmt: skip
            published = sent.verdict
            if sent.local and published.decision in BLOCKING:
                return ProcessResult(
                    delivery.message_id, current_id, "unavailable", False, published
                )
            if published.decision in BLOCKING:
                return await self._finish(delivery, current_id, "blocked", published)
            if published.decision == "RE_GROUND":
                attempt += 1
                if attempt > self.cl.max_regrounds:  # pragma: no cover - service blocks first
                    return await self._finish(delivery, current_id, "blocked", published)
                reminder = _reminder(published)
                current_id = retry_hop_id(base_id, attempt)
                continue
            message_id = await self.transport.publish(
                self.out_stream, self.adapter.write_envelope(incoming, emitted)
            )
            return await self._finish(delivery, current_id, "done", published, message_id)

    def _envelope(self, incoming: Envelope, hop_id: str, text: str) -> Envelope:
        return next_envelope(
            incoming, hop_id=hop_id, agent_id=self.agent_id, text=text,
            emitted_at=self._clock.now(),
        )  # fmt: skip

    async def _finish(
        self,
        delivery: Delivery,
        hop_id: str,
        outcome: Outcome,
        verdict: Verdict | None,
        published_id: str | None = None,
    ) -> ProcessResult:
        await self.transport.ack(self.stream, self.group, delivery.message_id)
        return ProcessResult(delivery.message_id, hop_id, outcome, True, verdict, published_id)


def _reminder(verdict: Verdict) -> str:
    if verdict.decision != "RE_GROUND" or not verdict.regrounded_text:
        return ""
    return f"\n\nConstraint reminder: {verdict.regrounded_text}"
