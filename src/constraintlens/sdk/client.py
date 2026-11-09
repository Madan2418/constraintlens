"""ConstraintLens SDK: the only thing a pipeline author touches (ARCHITECTURE.md §7.3, §10).

    cl = ConstraintLens(base_url="http://localhost:8000")
    trace = await cl.open_trace("Refund this customer, up to $100 ...", owner="user:alice")

    @cl.guard_hop(agent_id="triage", adapter=LangGraphStateAdapter())
    async def triage(state): ...          # agent logic unchanged

    @cl.guard_tool(name="issue_refund", binding=ActionBinding(action="refund", ...))
    async def issue_refund(order: int, amount: float): ...

* ``guard_hop`` runs CONSUME before the agent and PUBLISH after it. It re-grounds the
  agent with the canonical constraint text when asked (bounded retries, new hop id per
  attempt, ADR 0005).
* ``guard_tool`` runs the TOOL_CALL gate. On BLOCK or ESCALATE it raises
  ``ConstraintViolation`` and the tool does not execute (§7.8).
* Availability: HTTP calls have timeouts and a local circuit breaker. When the service
  cannot answer, ``on_unavailable`` decides per risk class: ``closed`` → a local,
  degraded BLOCK; ``open`` → a local ALLOW_WITH_WARNING (§10.1, §10.2).
* Redelivery (§9.6, ADR 0006): the service marks a stored verdict as replayed
  (``Idempotent-Replayed: true``). A replayed *allowing* TOOL_CALL verdict means this
  exact gated call was already authorized once, by an earlier delivery of the same
  message. ``guard_tool`` then raises ``DuplicateToolCall`` instead of executing the
  tool again, unless ``reexecute_redelivered_tools=True``.
* A *held* verdict (a missing predecessor, §9.4) raises ``CheckpointHeld``: the message
  must not be acknowledged, so the transport redelivers it and it is checked again.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, ParamSpec, TypeVar
from uuid import UUID, uuid4

import httpx

from constraintlens.domain.errors import ConstraintViolation as _DomainViolation
from constraintlens.domain.models import (
    ActionBinding,
    CheckpointRequest,
    ConstraintSpec,
    Envelope,
    HopMeta,
    ProposedAction,
    TransportMeta,
    Verdict,
)
from constraintlens.domain.projections import retry_hop_id

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from constraintlens.domain.models import Checkpoint

REPLAYED_HEADER = "Idempotent-Replayed"
P = ParamSpec("P")
R = TypeVar("R")
M = TypeVar("M")
FailMode = Literal["open", "closed"]
BLOCKING = frozenset({"BLOCK", "ESCALATE"})


class ConstraintViolation(_DomainViolation):
    """Raised at a guarded checkpoint whose verdict blocks; carries the verdict."""

    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict
        super().__init__(
            f"{verdict.decision} at {verdict.checkpoint} ({verdict.hop_id}): {verdict.reason}"
        )


class CheckpointHeld(ConstraintViolation):
    """The checkpoint waits for a missing predecessor (§9.4); retry by redelivery, not ack."""


class DuplicateToolCall(Exception):
    """A redelivered message re-issued a tool call that was already authorized (§9.6)."""

    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict
        super().__init__(
            f"{verdict.hop_id}: tool call already authorized by verdict {verdict.verdict_id}; "
            "not executed again"
        )


def raise_if_blocking(verdict: Verdict) -> None:
    """Raise for verdicts that must stop the caller: held, BLOCK or ESCALATE."""
    if verdict.held:
        raise CheckpointHeld(verdict)
    if verdict.decision in BLOCKING:
        raise ConstraintViolation(verdict)


@dataclass(frozen=True)
class CheckpointReply:
    """A verdict plus how it was obtained."""

    verdict: Verdict
    replayed: bool = False  # the service's stored verdict for an identical request (§9.6)
    local: bool = False  # a local fail-policy verdict: the service could not answer (§10.1)


class ConstraintLensClientError(Exception):
    """The service rejected the request itself (4xx): a bug in the caller, not an outage."""

    def __init__(self, status: int, problem: dict[str, Any]) -> None:
        self.status = status
        self.problem = problem
        super().__init__(f"{status} {problem.get('title', '')}: {problem.get('detail', '')}")


class ServiceUnavailable(Exception):
    """Raised by ``open_trace`` when the service cannot be reached (there is no local fallback)."""


@dataclass(frozen=True)
class OpenedTrace:
    trace_id: UUID
    envelope: Envelope
    conflicts: list[dict[str, Any]]
    extraction: dict[str, Any]


@dataclass
class HopContext:
    """The hop currently executing, for tools called inside it.

    Each tool call is its own checkpoint with a sub-hop id ``<hop>.t<n>`` (§4.1: a hop
    emits one delegation *or* one tool call). The n-th call of a re-executed agent gets
    the same id, so redelivery stays idempotent.
    """

    envelope: Envelope  # this hop's envelope (T_k = the task text the agent acts on)
    tool_calls: int = 0

    @property
    def hop_id(self) -> str:
        return self.envelope.hop.hop_id

    def next_tool_hop_id(self) -> str:
        self.tool_calls += 1
        return f"{self.hop_id}.t{self.tool_calls}"


_current_hop: contextvars.ContextVar[HopContext | None] = contextvars.ContextVar(
    "constraintlens_hop", default=None
)


def current_hop() -> HopContext | None:
    return _current_hop.get()


def next_envelope(
    parent: Envelope,
    *,
    hop_id: str,
    agent_id: str,
    text: str,
    args: dict[str, Any] | None = None,
    emitted_at: datetime | None = None,
) -> Envelope:
    """The envelope a hop emits: constraints and digest untouched (§7.2), new hop metadata."""
    return Envelope(
        trace_id=parent.trace_id,
        hop=HopMeta(
            hop_id=hop_id,
            parent_hop_ids=[parent.hop.hop_id],
            hop_seq=parent.hop.hop_seq + 1,
            agent_id=agent_id,
            emitted_at=emitted_at or datetime.now(UTC),
        ),
        constraints=list(parent.constraints),
        envelope_digest=parent.envelope_digest,
        payload_text=text,
        payload_args=args,
    )


class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; lets one trial through after ``reset_s``."""

    def __init__(
        self,
        threshold: int = 3,
        reset_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.reset_s = reset_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self._clock() - self._opened_at >= self.reset_s else "open"

    def allow(self) -> bool:
        return self.state != "open"

    def success(self) -> None:
        self._failures, self._opened_at = 0, None

    def failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold or self.state == "half_open":
            self._opened_at = self._clock()


class ConstraintLens:
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        api_key: str | None = None,
        timeout_s: float = 5.0,
        on_unavailable: dict[str, FailMode] | None = None,
        breaker: CircuitBreaker | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_regrounds: int = 2,
        reexecute_redelivered_tools: bool = False,
    ) -> None:
        headers = {"X-API-Key": api_key} if api_key else {}
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=timeout_s, headers=headers, transport=transport
        )
        self.on_unavailable: dict[str, FailMode] = {"high": "closed", "low": "open"}
        self.on_unavailable.update(on_unavailable or {})
        self.breaker = breaker or CircuitBreaker()
        self.max_regrounds = max_regrounds
        self.reexecute_redelivered_tools = reexecute_redelivered_tools

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> ConstraintLens:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ service calls

    async def open_trace(
        self,
        instruction: str,
        *,
        owner: str,
        declared: list[ConstraintSpec] | None = None,
        risk_class: str = "high",
    ) -> OpenedTrace:
        body = {
            "instruction": instruction,
            "owner": owner,
            "declared": [d.model_dump(mode="json") for d in declared or []],
            "risk_class": risk_class,
        }
        try:
            r = await self._http.post("/v1/traces", json=body)
        except httpx.HTTPError as exc:
            raise ServiceUnavailable(str(exc)) from exc
        data = self._json_or_raise(r)
        return OpenedTrace(
            trace_id=UUID(data["trace_id"]),
            envelope=Envelope.model_validate(data["envelope"]),
            conflicts=data["conflicts"],
            extraction=data["extraction"],
        )

    async def close_trace(self, trace_id: UUID) -> dict[str, Any]:
        return self._json_or_raise(await self._http.post(f"/v1/traces/{trace_id}/close"))

    async def get_trace(self, trace_id: UUID) -> dict[str, Any]:
        return self._json_or_raise(await self._http.get(f"/v1/traces/{trace_id}"))

    async def events(self, trace_id: UUID, *, after_event_id: int = 0) -> list[dict[str, Any]]:
        r = await self._http.get(
            f"/v1/traces/{trace_id}/events", params={"after_event_id": after_event_id}
        )
        if r.status_code >= 400:
            self._json_or_raise(r)
        data: list[dict[str, Any]] = r.json()
        return data

    async def replay(self, trace_id: UUID, **params: Any) -> dict[str, Any]:
        return self._json_or_raise(
            await self._http.post(f"/v1/traces/{trace_id}/replay", json=params)
        )

    async def checkpoint(
        self, request: CheckpointRequest, *, idempotency_key: str | None = None
    ) -> Verdict:
        """Submit a checkpoint. Never raises for outages: falls back per ``on_unavailable``."""
        return (await self.checkpoint_ex(request, idempotency_key=idempotency_key)).verdict

    async def checkpoint_ex(
        self, request: CheckpointRequest, *, idempotency_key: str | None = None
    ) -> CheckpointReply:
        """``checkpoint`` plus whether the verdict was replayed or decided locally."""
        if not self.breaker.allow():
            return CheckpointReply(self._fallback(request, "circuit breaker open"), local=True)
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        try:
            r = await self._http.post(
                f"/v1/traces/{request.trace_id}/checkpoints",
                content=request.model_dump_json(),
                headers={"Content-Type": "application/json", **headers},
            )
        except httpx.HTTPError as exc:
            self.breaker.failure()
            why = f"{exc.__class__.__name__}: {exc}"
            return CheckpointReply(self._fallback(request, why), local=True)
        if r.status_code >= 500:
            self.breaker.failure()
            why = f"service returned {r.status_code}"
            return CheckpointReply(self._fallback(request, why), local=True)
        self.breaker.success()
        replayed = r.headers.get(REPLAYED_HEADER, "").lower() == "true"
        return CheckpointReply(Verdict.model_validate(self._json_or_raise(r)), replayed)

    def _json_or_raise(self, r: httpx.Response) -> dict[str, Any]:
        if r.status_code >= 400:
            try:
                problem = r.json()
            except ValueError:
                problem = {"title": r.reason_phrase, "detail": r.text[:500]}
            raise ConstraintLensClientError(r.status_code, problem)
        data: dict[str, Any] = r.json()
        return data

    def _fallback(self, request: CheckpointRequest, why: str) -> Verdict:
        """Local degraded verdict when the service cannot answer (§10.1)."""
        risks = {c.risk_class for c in request.envelope.constraints} or {"high"}
        closed = any(self.on_unavailable[r] == "closed" for r in risks)
        return Verdict(
            verdict_id=uuid4(),
            trace_id=request.trace_id,
            hop_id=request.hop_id,
            checkpoint=request.checkpoint,
            tier_used="T0",
            scores={},
            failures=[],
            decision="BLOCK" if closed else "ALLOW_WITH_WARNING",
            degraded=True,
            reason=f"ConstraintLens unavailable ({why}); failing "
            f"{'closed' if closed else 'open'} per on_unavailable",
            latency_ms={},
            cache_hit=False,
            regrounded_text=None,
        )

    # ------------------------------------------------------------------ checkpoints

    async def consume(
        self, envelope: Envelope, *, hop_id: str, transport_meta: TransportMeta | None = None
    ) -> Verdict:
        return await self._submit("CONSUME", envelope, hop_id, transport_meta=transport_meta)

    async def publish(self, envelope: Envelope, *, destination: str | None = None) -> Verdict:
        """Egress check. ``destination`` names the queue the envelope is about to enter;
        the service then expects it to be consumed (transit-loss sweeper, §9.5 d)."""
        meta = TransportMeta(destination=destination) if destination else None
        return await self._submit("PUBLISH", envelope, envelope.hop.hop_id, transport_meta=meta)

    async def tool_call(
        self, envelope: Envelope, action: ProposedAction, *, hop_id: str | None = None
    ) -> Verdict:
        return await self._submit(
            "TOOL_CALL", envelope, hop_id or envelope.hop.hop_id, proposed_action=action
        )

    async def _submit(
        self,
        checkpoint: Checkpoint,
        envelope: Envelope,
        hop_id: str,
        *,
        proposed_action: ProposedAction | None = None,
        transport_meta: TransportMeta | None = None,
    ) -> Verdict:
        reply = await self._submit_ex(
            checkpoint, envelope, hop_id,
            proposed_action=proposed_action, transport_meta=transport_meta,
        )  # fmt: skip
        return reply.verdict

    async def _submit_ex(
        self,
        checkpoint: Checkpoint,
        envelope: Envelope,
        hop_id: str,
        *,
        proposed_action: ProposedAction | None = None,
        transport_meta: TransportMeta | None = None,
    ) -> CheckpointReply:
        return await self.checkpoint_ex(
            CheckpointRequest(
                trace_id=envelope.trace_id,
                hop_id=hop_id,
                checkpoint=checkpoint,
                envelope=envelope,
                proposed_action=proposed_action,
                transport_meta=transport_meta,
            )
        )

    @asynccontextmanager
    async def hop(self, envelope: Envelope) -> AsyncIterator[HopContext]:
        """Make ``envelope`` the current hop for guarded tools called inside the block."""
        ctx = HopContext(envelope)
        token = _current_hop.set(ctx)
        try:
            yield ctx
        finally:
            _current_hop.reset(token)

    # ------------------------------------------------------------------ decorators

    def guard_tool(
        self, *, name: str, binding: ActionBinding | None = None
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        """TOOL_CALL gate (§6.4): the decorated tool runs only if the verdict allows it."""

        def decorate(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
            signature = inspect.signature(fn)

            @functools.wraps(fn)
            async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                ctx = _current_hop.get()
                if ctx is None:
                    raise RuntimeError(
                        f"guarded tool {name!r} called outside a hop; use guard_hop or cl.hop()"
                    )
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                call_args: dict[str, Any] = {}
                for pname, value in bound.arguments.items():
                    if signature.parameters[pname].kind is inspect.Parameter.VAR_KEYWORD:
                        call_args.update(value)  # flatten **kwargs into named arguments
                    else:
                        call_args[pname] = value
                action = ProposedAction(tool=name, args=call_args, binding=binding)
                reply = await self._submit_ex(
                    "TOOL_CALL", ctx.envelope, ctx.next_tool_hop_id(), proposed_action=action
                )
                verdict = reply.verdict
                if verdict.decision in BLOCKING or verdict.decision == "RE_GROUND":
                    raise ConstraintViolation(verdict)
                if reply.replayed and not self.reexecute_redelivered_tools:
                    raise DuplicateToolCall(verdict)
                return await fn(*args, **kwargs)

            return wrapper

        return decorate

    def guard_hop(
        self, *, agent_id: str, adapter: Any, hop_id: Callable[[Envelope], str] | None = None
    ) -> Callable[[Callable[[M], Awaitable[M]]], Callable[[M], Awaitable[M]]]:
        """CONSUME before the agent, PUBLISH after it; re-ground on request (§6.4, §7.8).

        ``adapter`` reads/writes the envelope and the task text in the framework's
        message (e.g. ``LangGraphStateAdapter``). ``hop_id`` derives this hop's id from
        the incoming envelope (default ``h<seq>``).
        """
        make_hop_id = hop_id or (lambda env: f"h{env.hop.hop_seq + 1}")

        def decorate(fn: Callable[[M], Awaitable[M]]) -> Callable[[M], Awaitable[M]]:
            @functools.wraps(fn)
            async def wrapper(message: M) -> M:
                incoming = adapter.read_envelope(message)
                if incoming is None:
                    raise ConstraintViolation(_local_block(agent_id, "envelope missing"))
                base_id = make_hop_id(incoming)
                consumed = await self.consume(incoming, hop_id=base_id)
                raise_if_blocking(consumed)

                attempt, current_id, task_message = 0, base_id, message
                while True:
                    acting = next_envelope(
                        incoming, hop_id=current_id, agent_id=agent_id,
                        text=adapter.read_text(task_message),
                    )  # fmt: skip
                    async with self.hop(acting):
                        out = await fn(task_message)
                    emitted = next_envelope(
                        incoming, hop_id=current_id, agent_id=agent_id,
                        text=adapter.read_text(out),
                    )  # fmt: skip
                    published = await self.publish(emitted)
                    if published.decision in BLOCKING:
                        raise ConstraintViolation(published)
                    if published.decision != "RE_GROUND":
                        return adapter.write_envelope(out, emitted)  # type: ignore[no-any-return]
                    attempt += 1
                    if attempt > self.max_regrounds:  # pragma: no cover - service blocks first
                        raise ConstraintViolation(published)
                    reminder = f"\n\nConstraint reminder: {published.regrounded_text}"
                    task_message = adapter.write_text(
                        message, adapter.read_text(message) + reminder
                    )
                    current_id = retry_hop_id(base_id, attempt)

            return wrapper

        return decorate


def _local_block(agent_id: str, why: str) -> Verdict:
    return Verdict(
        verdict_id=uuid4(),
        trace_id=UUID(int=0),
        hop_id=agent_id,
        checkpoint="CONSUME",
        tier_used="T0",
        scores={},
        failures=[],
        decision="BLOCK",
        degraded=True,
        reason=f"{why}: cannot verify constraints for {agent_id}; failing closed",
        latency_ms={},
        cache_hit=False,
        regrounded_text=None,
    )
