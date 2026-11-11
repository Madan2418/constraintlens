"""Scripted agent step, run records, and a virtual clock shared by both pipelines.

Agents are deterministic so a run is reproducible from ``(scenario, mutation, hop,
org_policy)``. A real-LLM agent mode can plug in later behind the same ``agent_step``
signature (the live-pipeline trials of §14.4 need it); the demo and CI do not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pipelines.common.facts import Mutation, TaskFacts, mutate, parse

if TYPE_CHECKING:
    from pipelines.common.scenarios import Scenario, ToolCall

START = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


class VirtualClock:
    """Simulated time, so a 12-minute queue dwell costs nothing in tests and demos."""

    def __init__(self, start: datetime = START) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("time cannot move backwards")
        self._now += timedelta(seconds=seconds)


@dataclass(frozen=True)
class HopRecord:
    hop: int
    agent: str
    text: str
    emitted_at: datetime
    queue_dwell_s: float = 0.0
    delivery: int = 1
    mutated: bool = False


@dataclass
class RunResult:
    pipeline: str
    scenario: str
    trace_id: str
    instruction: str
    mutation: Mutation
    mutated_hop: int | None
    org_policy: bool
    hops: list[HopRecord] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    blocked: list[tuple[ToolCall, str]] = field(default_factory=list)  # protected runs only
    halted: str | None = None  # a CONSUME / PUBLISH checkpoint stopped the pipeline
    guard_notes: list[str] = field(default_factory=list)  # protected runs: holds, duplicates
    attribution: list[str] = field(default_factory=list)  # protected runs: culprit hops (§7.7)

    @property
    def silent_violation(self) -> bool:
        """A constraint was broken and nothing in the pipeline noticed or raised."""
        return bool(self.violations) and not self.errors


def agent_step(
    scenario: Scenario,
    hop: int,
    incoming_text: str,
    *,
    mutation: Mutation,
    mutated_hop: int | None,
) -> tuple[TaskFacts, str, bool]:
    """One agent reads the text it was given, (maybe) drifts, and writes its own text."""
    role = scenario.roles[hop - 1]
    facts = parse(incoming_text, wide_phrase=scenario.wide_phrase)
    drifted = mutation != "NONE" and hop == mutated_hop
    if drifted:
        facts = mutate(facts, mutation)
    return facts, scenario.render(role, facts), drifted


def render_report(result: RunResult) -> str:
    """Console view of a run, in the style of ARCHITECTURE.md §2.1."""
    lines = [
        f"[{result.pipeline}] scenario={result.scenario} mutation={result.mutation}"
        + (f" at hop {result.mutated_hop}" if result.mutated_hop else "")
        + (" (+org policy)" if result.org_policy else ""),
        "",
        f"User:    {result.instruction!r}",
        "",
    ]
    for h in result.hops:
        if h.queue_dwell_s:
            d = h.queue_dwell_s
            dwell = f"{d / 60:.0f} minute" if d >= 60 else f"{d:.0f} second"
            lines.append(f"        -- queue, {dwell} delay --")
        flag = "   <- drift injected here" if h.mutated else ""
        if h.delivery > 1:
            flag += f"   <- redelivery #{h.delivery} (previous worker died before ack)"
        lines.append(f"Hop {h.hop}  {h.agent:<11} T{h.hop}: {h.text!r}{flag}")
    lines.append("")
    for c in result.tool_calls:
        args = ", ".join(f"{k}={v}" for k, v in c.args.items())
        lines.append(f"Tool call (executed): {c.tool}({args})")
    for c, reason in result.blocked:
        args = ", ".join(f"{k}={v}" for k, v in c.args.items())
        lines.append(f"Tool call BLOCKED by ConstraintLens (did not execute): {c.tool}({args})")
        lines.append(f"  reason: {reason}")
    if result.halted:
        lines.append(f"Pipeline halted by ConstraintLens: {result.halted}")
    lines += [f"ConstraintLens: {n}" for n in result.guard_notes]
    lines += [f"Attribution: {a}" for a in result.attribution]
    lines.append("")
    if result.violations:
        lines.append("Ground truth: VIOLATION of the user's constraint")
        lines += [f"  - {v}" for v in result.violations]
        lines.append(
            "Pipeline: no error, no warning, nothing logged."
            if result.silent_violation
            else "Pipeline raised: " + "; ".join(result.errors)
        )
    else:
        lines.append("Ground truth: all executed actions respect the user's constraint.")
        if (result.blocked or result.halted) and result.mutation != "NONE":
            lines.append("ConstraintLens stopped the drifted action before it executed.")
        elif result.blocked or result.halted:
            lines.append("ConstraintLens stopped the pipeline before it acted (reason above).")
    return "\n".join(lines)


def attribution_lines(closed: dict[str, Any]) -> list[str]:
    """The attribution card (§7.7) of a closed trace, one line per failed constraint."""
    texts = {c["constraint_id"]: c["canonical_text"] for c in closed.get("constraints", [])}
    lines = []
    for a in closed.get("attributions", []):
        where = a["culprit_hop"] or "no single hop"
        who = (
            f" ({a['culprit_agent']})"
            if a["culprit_agent"]
            else (" (queue edge)" if a["culprit_hop"] else "")
        )
        lines.append(
            f"{where}{who} lost {texts.get(a['constraint_id'], a['constraint_id'])!r}; "
            f"cause {a['cause']}, delta {a['delta']:.2f}"
        )
    return lines
