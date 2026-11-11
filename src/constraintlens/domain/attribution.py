"""Drift attribution: which hop introduced a failure (ARCHITECTURE.md §4.2, §7.7, ADR 0009).

Attribution is a pure **view over the event log** (§5, §12.1). It reads the recorded
checkpoint requests and the verdicts issued for them, so it can be recomputed offline
with new parameters (``theta``, ``delta_min``), or over replayed verdicts, without
touching the trace.

**Model (§7.7).** A trace is a DAG of hops. Every envelope a hop emits carries its task
text T_k, and every checkpoint that checks T_k contributes to the hop's entailment
vector ``E_k[c]``:

* a score from a semantic tier (1c / 2) is used as is;
* a check that reported a drift failure on ``c`` and gave no score counts as 0.0;
* a check that passed ``c`` without a score (Tiers 0 / 1a / 1b only) counts as 1.0,
  i.e. "no evidence of drift". This keeps the algorithm meaningful without a semantic
  tier, at the price of {0, 1} deltas.

``E_k[c]`` is the minimum over all checks of T_k (PUBLISH, TOOL_CALL, and CONSUME of the
same text downstream). The root hop's text is T_0, from which the constraints were
issued, so ``E_root = 1.0``.

**Failure points**, visited in topological order (parents before children, consumption
before emission inside a hop):

* *emission of hop k*: a drift failure on ``c`` (``TEXTUAL_DRIFT``, ``CONSTRAINT_LOST``,
  ``CONSTRAINT_DROPPED``, ``HASH_MISMATCH``, ``EXPIRED``) or ``E_k[c] < theta``;
* *queue edge p → k* (§7.7 step 4): at k's CONSUME of p's envelope, ``c`` is stale and
  was not re-verified, or failed re-verification (``STALE``), or expired: cause
  ``STALE_CONTEXT``. The envelope lost or changed constraints, or the consumed text
  differs from what p published: cause ``TRANSIT_LOSS``. Either way the culprit is
  the consuming hop's *edge*, so ``culprit_agent`` is ``None``;
* *transit loss case (d)*: the sweeper found p's publish never consumed (§9.5):
  ``TRANSIT_LOSS`` on the edge out of p.

**Culprit (§7.7 steps 2-3).** The first failure point for ``c``. An emission point is
the culprit only if its incoming delta ``Δ = baseline − E_k[c]`` exceeds ``delta_min``,
where the baseline is the *minimum* parent score (fan-in). If not, the hop merely
matches its worst parent and blame propagates to that parent, then on up, until a hop
whose own drop exceeds ``delta_min``. If none does, the drift was gradual and no single
hop is blamed (``culprit_hop=None``, ``UNKNOWN``).

**Cause.** ``STALE_CONTEXT`` / ``TRANSIT_LOSS`` for edge points and expiry; otherwise
``INJECTION`` when the culprit hop's text introduces an attributed note that tries to
override the constraint ("CRM note: ignore previous limits", §21.1) and no parent's
text had one, else ``REWRITE``.

**Violations without drift (§4.2).** If ``c`` never drifted but a TOOL_CALL violated it,
the culprit is where the bad action was proposed, unless an ancestor introduced an
injected override, which is then blamed with ``INJECTION``. Otherwise the cause is
``UNKNOWN``: the action broke the constraint while every text still entailed it.

Pure module: standard library, pydantic models, and other domain modules only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from constraintlens.domain.async_guard import GuardConfig, stale_constraints
from constraintlens.domain.models import (
    Attribution,
    AttributionCause,
    CheckpointRequest,
    Verdict,
)
from constraintlens.domain.projections import TraceState, base_hop_id

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from uuid import UUID

    from constraintlens.domain.models import Event, Failure

#: Failures that say the meaning of a constraint was lost in T_k or its envelope.
DRIFT_KINDS = frozenset(
    {"TEXTUAL_DRIFT", "CONSTRAINT_LOST", "CONSTRAINT_DROPPED", "HASH_MISMATCH", "EXPIRED", "STALE"}
)
CARRIER_KINDS = frozenset({"CONSTRAINT_LOST", "CONSTRAINT_DROPPED", "HASH_MISMATCH"})
TIME_KINDS = frozenset({"STALE", "EXPIRED"})
EMISSION_CHECKPOINTS = frozenset({"PUBLISH", "TOOL_CALL"})

# --------------------------------------------------------------------------- #
# Injected-instruction marker (§21.1)
# --------------------------------------------------------------------------- #

#: A note attributed to a tool or a third party: "CRM note:", "Query tool output:".
_ATTRIBUTED = re.compile(
    r"(?:^|(?<=[.;!?\n]))\s*((?:[A-Z][\w-]*\s+(?:[a-z][\w-]*\s+){0,2})?"
    r"(?i:note|output|reply|message|result|response|quote|comment|email|notice)s?\s*:"
    r"[^\n]*?(?:[.!?](?=\s|$)|$))",
)
#: Words in such a note that try to lift or overrule a limit.
_OVERRIDE = re.compile(
    r"\b(?:ignore|disregard|override|bypass|skip)\b[^.;]{0,40}?"
    r"\b(?:limits?|caps?|ceilings?|budgets?|instructions?|rules?|polic\w*|constraints?|approvals?)\b"
    r"|\bno\s+longer\s+applies\b|\bin\s+full\b|\bfull\s+amount\b"
    r"|\balready\s+(?:been\s+)?(?:approved|cleared|authori[sz]ed)\b"
    r"|\bis\s+compliant\b|\bpre-?approved\b|\bwithin\s+the\s+approved\s+limit\b",
    re.IGNORECASE,
)


def injection_marker(text: str) -> str | None:
    """The first attributed note in ``text`` that tries to override a limit, if any."""
    for m in _ATTRIBUTED.finditer(text):
        note = m.group(1).strip()
        if _OVERRIDE.search(note):
            return note
    return None


# --------------------------------------------------------------------------- #
# Parameters and inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AttributionParams:
    """§7.7 parameters. ``theta`` and ``delta_min`` are tuned on ``val`` (§13.7)."""

    theta: float = 0.35
    delta_min: float = 0.15
    guard: GuardConfig = field(default_factory=GuardConfig)  # staleness, as in the live check

    def __post_init__(self) -> None:
        for name in ("theta", "delta_min"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must lie in [0, 1], got {value}")

    def as_dict(self) -> dict[str, float]:
        return {
            "theta": self.theta,
            "delta_min": self.delta_min,
            "dwell_threshold_s": self.guard.dwell_threshold_s,
        }


@dataclass(frozen=True)
class CheckOutcome:
    """What the checker concluded at one checkpoint (a verdict, recorded or replayed)."""

    scores: Mapping[UUID, float]
    failures: Sequence[Failure]
    final: bool = True  # held and conflicting checks are not the checkpoint's answer
    verdict_event_id: int | None = None

    @classmethod
    def of(cls, verdict: Verdict, *, event_id: int | None = None) -> CheckOutcome:
        return cls(verdict.scores, verdict.failures, not verdict.held, event_id)


# --------------------------------------------------------------------------- #
# The trace graph, built from the log
# --------------------------------------------------------------------------- #


@dataclass
class _Mark:
    """One checkpoint's evidence about one constraint at one point."""

    score: float | None
    failed: bool
    cause: AttributionCause | None  # set for time / transit causes
    evidence: tuple[int, ...]


@dataclass
class _Node:
    hop: str
    seq: int
    first_event: int
    agent: str | None = None
    parents: set[str] = field(default_factory=set)
    texts: list[str] = field(default_factory=list)  # emitted texts T_k (all attempts)
    marks: dict[UUID, list[_Mark]] = field(default_factory=dict)  # emission checks
    #: inbound queue edges: parent -> constraint -> marks
    edges: dict[str, dict[UUID, list[_Mark]]] = field(default_factory=dict)
    lost: dict[UUID, list[_Mark]] = field(default_factory=dict)  # §9.5 (d) out of this hop
    violations: dict[UUID, list[tuple[int, ...]]] = field(default_factory=dict)


@dataclass
class TraceGraph:
    """Hops, edges and per-constraint evidence, in the order the log recorded them."""

    root: str | None
    constraint_ids: list[UUID]
    nodes: dict[str, _Node]
    last_event_id: int

    def order(self) -> list[_Node]:
        """Topological order; ties by hop_seq, then by first appearance in the log."""
        indeg = {h: sum(p in self.nodes for p in n.parents) for h, n in self.nodes.items()}
        children: dict[str, list[str]] = {h: [] for h in self.nodes}
        for h, n in self.nodes.items():
            for p in n.parents:
                if p in self.nodes:
                    children[p].append(h)
        ready = [n for h, n in self.nodes.items() if indeg[h] == 0]
        out: list[_Node] = []
        while ready:
            ready.sort(key=lambda n: (n.seq, n.first_event, n.hop))
            node = ready.pop(0)
            out.append(node)
            for child in children[node.hop]:
                indeg[child] -= 1
                if indeg[child] == 0:
                    ready.append(self.nodes[child])
        # A cycle cannot be recorded (HopMeta forbids self-parents, and parents are earlier
        # hops in practice); keep any leftovers so no evidence is silently dropped.
        out += sorted(
            (n for n in self.nodes.values() if n not in out),
            key=lambda n: (n.seq, n.first_event, n.hop),
        )
        return out


def build_graph(
    trace_id: UUID,
    events: Iterable[Event],
    *,
    guard: GuardConfig | None = None,
    outcomes: Mapping[int, CheckOutcome] | None = None,
) -> TraceGraph:
    """Fold the log into a ``TraceGraph``.

    ``outcomes`` maps a ``CHECKPOINT_RECEIVED`` event id to the outcome to use instead of
    the recorded verdict (replay with other tiers or thresholds). Each checkpoint is read
    against the state *as it was when it arrived*, as replay does.
    """
    guard = guard or GuardConfig()
    at = TraceState(trace_id=trace_id)
    nodes: dict[str, _Node] = {}
    root: str | None = None
    pending: tuple[int, CheckpointRequest, dict[UUID, str]] | None = None

    def node(hop: str, seq: int, event_id: int) -> _Node:
        n = nodes.get(hop)
        if n is None:
            n = nodes[hop] = _Node(hop=hop, seq=seq, first_event=event_id)
        else:
            n.seq = min(n.seq, seq)
        return n

    def observe(
        event_id: int, req: CheckpointRequest, stale: dict[UUID, str], o: CheckOutcome
    ) -> None:
        if not o.final:
            return
        evidence = (event_id, o.verdict_event_id) if o.verdict_event_id else (event_id,)
        env = req.envelope
        text_hop = base_hop_id(env.hop.hop_id)
        src = node(text_hop, env.hop.hop_seq, event_id)
        src.parents |= {base_hop_id(p) for p in env.hop.parent_hop_ids}
        ids = [c.constraint_id for c in at.constraints]
        by_c: dict[UUID, list[Failure]] = {cid: [] for cid in ids}
        for f in o.failures:
            if f.kind == "ACTION_VIOLATION" and f.constraint_id in by_c:
                if req.checkpoint == "TOOL_CALL":
                    src.violations.setdefault(f.constraint_id, []).append(evidence)
                continue
            if f.kind not in DRIFT_KINDS:
                continue  # ordering / gap findings are not about a constraint's meaning
            for cid in [f.constraint_id] if f.constraint_id is not None else ids:
                if cid in by_c:
                    by_c[cid].append(f)

        src.agent = src.agent or env.hop.agent_id  # the envelope names T_k's author
        if req.checkpoint in EMISSION_CHECKPOINTS:
            if env.payload_text not in src.texts:
                src.texts.append(env.payload_text)
            for cid, fs in by_c.items():
                expired = any(f.kind == "EXPIRED" for f in fs)
                src.marks.setdefault(cid, []).append(
                    _Mark(o.scores.get(cid), bool(fs), "STALE_CONTEXT" if expired else None,
                          evidence)
                )  # fmt: skip
            return

        # CONSUME: the actor reads the parent's envelope across a queue edge.
        actor = node(base_hop_id(req.hop_id), env.hop.hop_seq + 1, event_id)
        if actor.hop == text_hop:  # a hop re-reading its own envelope: no edge to blame
            return
        actor.parents.add(text_hop)
        edge = actor.edges.setdefault(text_hop, {})
        published = list(src.texts)
        altered = bool(published) and env.payload_text not in published
        for cid, fs in by_c.items():
            score = o.scores.get(cid)
            kinds = {f.kind for f in fs}
            reverified = score is not None and not fs
            cause: AttributionCause | None = None
            if kinds & TIME_KINDS or (cid in stale and not reverified):
                cause = "STALE_CONTEXT"
            elif kinds & CARRIER_KINDS or (fs and altered):
                cause = "TRANSIT_LOSS"
            if cause is not None:
                edge.setdefault(cid, []).append(_Mark(score, True, cause, evidence))
            else:
                # The same text the parent published: one more check of T_parent.
                src.marks.setdefault(cid, []).append(_Mark(score, bool(fs), None, evidence))

    for event in events:
        if event.trace_id != trace_id:
            continue
        if event.type == "TRACE_OPENED" and event.hop_id:
            root = base_hop_id(event.hop_id)
            n = node(root, 0, event.event_id)
            owner = event.payload.get("owner")
            n.agent = owner if isinstance(owner, str) else None  # T_0's author: the user
            text = event.payload.get("instruction")
            if isinstance(text, str):
                n.texts.append(text)
        elif event.type == "CHECKPOINT_RECEIVED":
            p = event.payload
            req = CheckpointRequest.model_validate(p["request"])
            stale = stale_constraints(req, at.constraints, event.occurred_at, guard)
            if p.get("conflict"):
                pass  # not the checkpoint's answer, and not trusted with history (ADR 0006)
            elif outcomes is not None:
                replayed = outcomes.get(event.event_id)
                if replayed is not None:
                    observe(event.event_id, req, stale, replayed)
            elif not p.get("held"):
                pending = (event.event_id, req, stale)
        elif event.type == "VERDICT_ISSUED" and pending is not None:
            # Recorded in the same batch as the checkpoint it answers (§7.9).
            event_id, req, stale = pending
            pending = None
            verdict = Verdict.model_validate(event.payload["verdict"])
            observe(event_id, req, stale, CheckOutcome.of(verdict, event_id=event.event_id))
        elif event.type == "WARNING" and event.payload.get("transit_loss"):
            hop = base_hop_id(str(event.payload.get("published_hop_id") or ""))
            if hop:
                n = node(hop, 0, event.event_id)
                for c in at.constraints:
                    n.lost.setdefault(c.constraint_id, []).append(
                        _Mark(None, True, "TRANSIT_LOSS", (event.event_id,))
                    )
        at.apply(event)
    return TraceGraph(
        root=root,
        constraint_ids=[c.constraint_id for c in at.constraints],
        nodes=nodes,
        last_event_id=at.last_event_id,
    )


# --------------------------------------------------------------------------- #
# Culprit search (§7.7 steps 1-5)
# --------------------------------------------------------------------------- #


def _value(mark: _Mark) -> float:
    if mark.score is not None:
        return mark.score
    return 0.0 if mark.failed else 1.0


class _Scorer:
    """Entailment of each hop for one constraint, with fan-in baselines."""

    def __init__(self, graph: TraceGraph, cid: UUID, params: AttributionParams) -> None:
        self.graph = graph
        self.cid = cid
        self.params = params
        self._eff: dict[str, float] = {}

    def marks(self, n: _Node) -> list[_Mark]:
        return n.marks.get(self.cid, [])

    def score(self, n: _Node) -> float | None:
        """E_k[c] from the checks of T_k, or ``None`` if T_k was never checked."""
        marks = self.marks(n)
        return min(_value(m) for m in marks) if marks else None

    def failing(self, n: _Node) -> bool:
        e = self.score(n)
        return any(m.failed for m in self.marks(n)) or (e is not None and e < self.params.theta)

    def parents(self, n: _Node) -> list[_Node]:
        return [self.graph.nodes[p] for p in sorted(n.parents) if p in self.graph.nodes]

    def baseline(self, n: _Node) -> float:
        """Minimum parent score (fan-in, step 3). T_0 entails 𝒞 by construction: 1.0."""
        ps = self.parents(n)
        return min(self.effective(p) for p in ps) if ps else 1.0

    def effective(self, n: _Node) -> float:
        """A hop's score, or, never checked, the score it inherited from its parents."""
        if n.hop not in self._eff:
            self._eff[n.hop] = 1.0  # cycle guard; overwritten below
            if n.hop == self.graph.root:
                self._eff[n.hop] = 1.0
            else:
                e = self.score(n)
                self._eff[n.hop] = e if e is not None else self.baseline(n)
        return self._eff[n.hop]

    def delta(self, n: _Node) -> float:
        e = self.score(n)
        return self.baseline(n) - (e if e is not None else self.effective(n))


def _evidence(*groups: Iterable[_Mark]) -> list[int]:
    return sorted({i for marks in groups for m in marks for i in m.evidence})


def _introduces_injection(n: _Node, graph: TraceGraph) -> bool:
    if not any(injection_marker(t) for t in n.texts):
        return False
    return not any(
        injection_marker(t) for p in n.parents if p in graph.nodes for t in graph.nodes[p].texts
    )


def _ancestors(n: _Node, graph: TraceGraph) -> set[str]:
    out: set[str] = set()
    stack = list(n.parents)
    while stack:
        h = stack.pop()
        if h not in out and h in graph.nodes:
            out.add(h)
            stack.extend(graph.nodes[h].parents)
    return out


def _emission_culprit(
    start: _Node, s: _Scorer, graph: TraceGraph, params: AttributionParams
) -> Attribution:
    """Step 2-3: the failing hop, unless it only matches its worst parent (propagate)."""
    trigger = s.marks(start)
    cur = start
    while s.delta(cur) <= params.delta_min:
        ps = s.parents(cur)
        if not ps or cur.hop == graph.root:
            # Gradual drift: no single hop lost more than delta_min.
            return Attribution(
                constraint_id=s.cid,
                culprit_hop=None,
                culprit_agent=None,
                cause="UNKNOWN",
                delta=round(1.0 - (s.score(start) or 0.0), 4),
                evidence_events=_evidence(trigger),
            )
        cur = min(ps, key=lambda p: (s.effective(p), p.seq, p.first_event))
    marks = s.marks(cur)
    cause: AttributionCause
    agent = cur.agent
    if any(m.cause == "STALE_CONTEXT" for m in marks if m.failed):
        cause, agent = "STALE_CONTEXT", None  # expired while held: time, not an agent
    elif _introduces_injection(cur, graph):
        cause = "INJECTION"
    else:
        cause = "REWRITE"
    return Attribution(
        constraint_id=s.cid,
        culprit_hop=cur.hop,
        culprit_agent=agent,
        cause=cause,
        delta=round(s.delta(cur), 4),
        evidence_events=_evidence(marks, trigger),
    )


def _violation_culprit(
    acting: _Node, evidence: list[tuple[int, ...]], s: _Scorer, graph: TraceGraph
) -> Attribution:
    """§4.2: no text lost the constraint, yet the action broke it."""
    ids = sorted({i for ev in evidence for i in ev})
    lineage = _ancestors(acting, graph) | {acting.hop}
    for n in graph.order():
        if n.hop in lineage and _introduces_injection(n, graph):
            return Attribution(
                constraint_id=s.cid,
                culprit_hop=n.hop,
                culprit_agent=n.agent,
                cause="INJECTION",
                delta=round(s.delta(n), 4),
                evidence_events=sorted({*ids, *_evidence(s.marks(n))}),
            )
    return Attribution(
        constraint_id=s.cid,
        culprit_hop=acting.hop,
        culprit_agent=acting.agent,
        cause="UNKNOWN",
        delta=round(s.delta(acting), 4),
        evidence_events=ids,
    )


def attribute_constraint(
    graph: TraceGraph, cid: UUID, params: AttributionParams
) -> Attribution | None:
    """The culprit for one constraint, or ``None`` if it never failed."""
    s = _Scorer(graph, cid, params)
    order = graph.order()
    for n in order:
        for parent in sorted(n.edges, key=lambda p: graph.nodes[p].first_event):
            marks = n.edges[parent].get(cid, [])
            if marks:
                cause = next(m.cause for m in marks if m.cause is not None)
                return Attribution(
                    constraint_id=cid,
                    culprit_hop=n.hop,  # the queue edge into this hop, not an agent
                    culprit_agent=None,
                    cause=cause,
                    delta=round(s.effective(graph.nodes[parent]) - min(map(_value, marks)), 4),
                    evidence_events=_evidence(marks),
                )
        if s.failing(n):
            return _emission_culprit(n, s, graph, params)
        lost = n.lost.get(cid, [])
        if lost:
            return Attribution(
                constraint_id=cid,
                culprit_hop=n.hop,  # the edge out of this hop: published, never consumed
                culprit_agent=None,
                cause="TRANSIT_LOSS",
                delta=round(s.effective(n), 4),
                evidence_events=_evidence(lost),
            )
    for n in order:
        if cid in n.violations:
            return _violation_culprit(n, n.violations[cid], s, graph)
    return None


def attribute(graph: TraceGraph, params: AttributionParams | None = None) -> list[Attribution]:
    """One ``Attribution`` per registered constraint that failed, in registration order."""
    params = params or AttributionParams()
    out = []
    for cid in graph.constraint_ids:
        a = attribute_constraint(graph, cid, params)
        if a is not None:
            out.append(a)
    return out


def attribute_events(
    trace_id: UUID,
    events: Iterable[Event],
    params: AttributionParams | None = None,
    *,
    outcomes: Mapping[int, CheckOutcome] | None = None,
) -> list[Attribution]:
    params = params or AttributionParams()
    graph = build_graph(trace_id, events, guard=params.guard, outcomes=outcomes)
    return attribute(graph, params)
