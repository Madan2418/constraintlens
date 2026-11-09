"""Constraint extraction (ARCHITECTURE.md §7.1, ADR 0003).

Pipeline, run once at trace open:

1. **Rule-based extractor** (fast, deterministic): finds actions, money and count
   limits, approval requirements, expiry windows and subject identifiers.
2. **LLM extractor** (fallback / enrichment) through the ``Judge`` port's
   structured-output call. Output is validated against a strict schema; invalid output
   gets one repair retry, then is rejected.
3. **Reconciliation**: where both paths produce a constraint for the same action,
   disagreements resolve to the *more restrictive* value and are flagged for review.

Canonicalization and hashing happen afterwards in ``canonical.issue``.

Pure module: standard library, pydantic and domain only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal

from pydantic import AwareDatetime, ConfigDict, Field, ValidationError

from constraintlens.domain.errors import DependencyUnavailable
from constraintlens.domain.models import (
    Authority,
    ConstraintSpec,
    Limit,
    Origin,
    RiskClass,
    Scope,
    _Frozen,
)
from constraintlens.domain.subject import SubjectScope

if TYPE_CHECKING:
    from constraintlens.domain.ports import Judge

LLMMode = Literal["off", "fallback", "always"]
Source = Literal["rule", "llm", "both"]
FlagKind = Literal[
    "LIMIT_DISAGREEMENT",
    "FIELD_DISAGREEMENT",
    "UNIT_DISAGREEMENT",
    "AMBIGUOUS_MATCH",
    "LLM_REJECTED",
    "LLM_UNAVAILABLE",
    "DUPLICATE_LIMIT",
]

# --------------------------------------------------------------------------- vocabulary

#: action -> (default resource, verb pattern). Order matters only for readability.
ACTIONS: dict[str, tuple[str, str]] = {
    "refund": ("order", r"\brefund(?:s|ed|ing)?\b|\breimburs(?:e|es|ed|ing|ement)\b"),
    "transfer": (
        "funds",
        r"\btransfer(?:s|red|ring)?\b|\bwire(?:s|d)?\b|\bremit(?:s|ted)?\b"
        r"|\bpay(?:s|ing)?\b|\bpayments?\b"
        r"|\bsend(?:s|ing)?\s+(?:the\s+)?(?:money|funds|payments?)\b",
    ),
    "delete": (
        "records",
        r"\bdelet(?:e|es|ed|ing|ion)\b|\bremov(?:e|es|ed|ing)\b|\bpurg(?:e|es|ed|ing)\b"
        r"|\beras(?:e|es|ed|ing)\b|\bwip(?:e|es|ed|ing)\b",
    ),
    "access": (
        "records",
        r"\baccess(?:es|ed|ing)?\b|\bread\b|\bview\b|\bexport(?:s|ed|ing)?\b|\bpull\b"
        r"|\bquery\b|\blook\s+up\b|\bretriev(?:e|es|ed|ing)\b|\bdownload(?:s|ed|ing)?\b",
    ),
    "schedule": (
        "meeting",
        r"\bschedul(?:e|es|ed|ing)\b|\binvit(?:e|es|ed|ing)\b|\bbook(?:s|ed|ing)?\b"
        r"|\bset\s+up\s+a\s+(?:meeting|call)\b",
    ),
    "purchase": (
        "purchase",
        r"\bpurchas(?:e|es|ed|ing)\b|\bbuy(?:s|ing)?\b|\bprocur(?:e|es|ed|ing|ement)\b",
    ),
}
_ACTION_RES = {a: re.compile(p, re.IGNORECASE) for a, (_, p) in ACTIONS.items()}
#: "Order 20 laptops" (imperative at sentence start) is a purchase; "order #4471" is not.
_ORDER_VERB = re.compile(r"^(?:please\s+)?order\b(?!\s*(?:#|no\.|number))", re.IGNORECASE)

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}
CURRENCY_WORDS = {
    "usd": "USD", "dollar": "USD", "dollars": "USD", "bucks": "USD",
    "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "gbp": "GBP", "pound": "GBP", "pounds": "GBP",
    "inr": "INR", "rs": "INR", "rs.": "INR", "rupee": "INR", "rupees": "INR",
}  # fmt: skip
COUNT_UNITS = {
    "record": "records", "records": "records", "row": "records", "rows": "records",
    "file": "files", "files": "files",
    "attendee": "attendees", "attendees": "attendees", "people": "attendees",
    "person": "attendees", "persons": "attendees", "guest": "attendees", "guests": "attendees",
    "account": "accounts", "accounts": "accounts", "item": "items", "items": "items",
}  # fmt: skip
SUBJECT_KINDS = {
    "order": "order", "invoice": "invoice", "account": "account", "customer": "customer",
    "ticket": "ticket", "case": "case", "po": "po", "vendor": "vendor",
    "supplier": "vendor", "record": "record", "employee": "employee",
}  # fmt: skip
MULTIPLIERS = {
    "k": 1_000, "thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "million": 1_000_000,
}  # fmt: skip

_NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_SUF = r"(?:\s?(k|thousand|million|lakhs?)\b)?"
_MONEY_RES = [
    # $100, €1.2k, ₹50,000
    (re.compile(rf"([$€£₹])\s?{_NUM}{_SUF}", re.IGNORECASE), "sym"),
    # USD 900, Rs 20000, INR 75,000
    (re.compile(rf"\b(usd|eur|gbp|inr|rs\.?)\s?{_NUM}{_SUF}", re.IGNORECASE), "pre"),
    # 75 dollars, 3k USD, 1,000 USD
    (
        re.compile(
            rf"\b{_NUM}{_SUF}\s?(usd|eur|gbp|inr|dollars?|euros?|pounds?|rupees?|bucks)\b",
            re.IGNORECASE,
        ),
        "post",
    ),
]
_COUNT_RE = re.compile(
    rf"\b{_NUM}{_SUF}\s+(?:[a-z]+\s+)?({'|'.join(sorted(COUNT_UNITS, key=len, reverse=True))})\b",
    re.IGNORECASE,
)
_TIME_QTY_RE = re.compile(
    rf"\b{_NUM}\s?(minutes?|mins?|hours?|hrs?|h|days?|weeks?)\b", re.IGNORECASE
)

# Cue phrases to the left of a quantity; the nearest one (longest on ties) wins.
_CUES: list[tuple[str, str]] = [
    ("max", r"up\s?to"),
    ("max", r"max(?:imum)?(?:\s+(?:of|spend))?"),
    ("max", r"at\s+most"),
    ("max", r"(?:no|not)\s+more\s+than"),
    ("max", r"(?:not|never|cannot|can't|must\s+not|shouldn't|should\s+not)"
            r"\s+(?:to\s+)?exceed(?:ing)?"),
    ("max", r"under"),
    ("max", r"below"),
    ("max", r"less\s+than"),
    ("max", r"limit(?:ed)?(?:\s+(?:of|to|at))?"),
    ("max", r"cap(?:ped)?(?:\s+(?:at|of))?"),
    ("max", r"budget(?:\s+(?:of|is))?:?"),
    ("max", r"ceiling(?:\s+of)?"),
    ("max", r"within"),
    ("min", r"at\s+least"),
    ("min", r"minimum(?:\s+of)?"),
    ("min", r"(?:no|not)\s+less\s+than"),
    ("above", r"above"),
    ("above", r"over"),
    ("above", r"exceed(?:s|ing)?"),
    ("above", r"beyond"),
    ("above", r"(?:more|greater|higher)\s+than"),
]  # fmt: skip
_CUE_RES = [(kind, re.compile(rf"\b(?:{p})(?!\w)", re.IGNORECASE)) for kind, p in _CUES]
_RIGHT_MAX = re.compile(
    r"^\s*(?:max(?:imum)?|tops|limit|cap|at\s+most|or\s+less|or\s+fewer)\b", re.I
)
_RIGHT_MIN = re.compile(r"^\s*(?:minimum|or\s+more|at\s+least)\b", re.IGNORECASE)
_NEGATION = re.compile(r"\b(?:don't|do\s+not|never|not|no|nothing|without)\b", re.IGNORECASE)

_APPROVAL = re.compile(
    r"\b(?:approv\w*|sign-?off|authori[sz]\w*|confirm\w*|permission|go-ahead)\b", re.IGNORECASE
)
_NO_APPROVAL = re.compile(
    r"\b(?:no|without|not)\s+(?:\w+\s+){0,2}?(?:approval|sign-?off|authori[sz]ation|confirmation)"
    r"\b(?:\s+(?:is\s+)?(?:needed|required))?"
    r"|\b(?:approval|sign-?off)\s+(?:is\s+)?not\s+(?:needed|required)\b",
    re.IGNORECASE,
)
_ABOVE_REF = re.compile(
    r"\b(?:anything|any\s+amount|amounts?|anything\s+else)\s+(?:above|over|more|beyond|higher|exceeding)\b"
    r"(?!\s*(?:[$€£₹]|\d|usd|eur|gbp|inr|rs))"
    r"|\b(?:above|over|beyond|exceeding)\s+(?:that|this|it|the\s+(?:limit|cap|budget))\b"
    r"|\b(?:approval|sign-?off)\s+(?:above|over)\b\s*(?:[.;,]|$)",
    re.IGNORECASE,
)
_RESTRICT = re.compile(r"\b(?:only|exclusively|solely)\b", re.IGNORECASE)

_REL_EXPIRY = re.compile(
    r"\b(?:within|in|(?:valid\s+)?for\s+the\s+next|valid\s+for|expires?\s+in|expiring\s+in"
    r"|over\s+the\s+next)\s+(\d+)\s?(minutes?|mins?|hours?|hrs?|h|days?|weeks?)\b",
    re.IGNORECASE,
)
_ABS_EXPIRY = re.compile(
    r"\b(?:by|before|until|no\s+later\s+than|due)\s+(\d{4}-\d{2}-\d{2})(?:[tT ](\d{2}:\d{2}))?"
)
_SUBJECT_RE = re.compile(
    rf"\b({'|'.join(SUBJECT_KINDS)})s?\b\s*(?:#|no\.|number|id)?\s*[:#-]?\s*([a-z]{{0,4}}-?\d[\w-]*)",
    re.IGNORECASE,
)
_PROJECT_RE = re.compile(r"\b(?i:project)\s+([A-Z][\w-]*)")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\s+[—–]\s+")

# --------------------------------------------------------------------------- results


class ReviewFlag(_Frozen):
    kind: FlagKind
    action: str | None = None
    detail: str


class ExtractionResult(_Frozen):
    specs: list[ConstraintSpec]
    sources: list[Source]  # parallel to ``specs``
    flags: list[ReviewFlag] = Field(default_factory=list)
    llm_called: bool = False
    degraded: bool = False  # the LLM path was wanted but unavailable or rejected


@dataclass(frozen=True)
class ExtractionContext:
    """Who issues the extracted constraints, and when (§8.1 ownership fields)."""

    owner: str
    issued_at: datetime
    scope: Scope = "user"
    origin: Origin = "user"
    risk_class: RiskClass = "high"
    max_age_seconds: int | None = None


# --------------------------------------------------------------------------- rule path


@dataclass
class _Quantity:
    start: int
    end: int
    value: Decimal
    unit: str
    role: str | None = None  # "max" | "min" | "above"


@dataclass
class _Context:
    action: str
    maxes: dict[str, list[Decimal]] = field(default_factory=dict)
    mins: dict[str, list[Decimal]] = field(default_factory=dict)
    thresholds: dict[str, list[Decimal]] = field(default_factory=dict)
    above_ref: bool = False
    always: bool = False
    restrict: bool = False
    subjects: set[str] = field(default_factory=set)
    expiries: list[datetime] = field(default_factory=list)

    def absorb(self, other: _Context) -> None:
        for mine, theirs in (
            (self.maxes, other.maxes),
            (self.mins, other.mins),
            (self.thresholds, other.thresholds),
        ):
            for unit, values in theirs.items():
                mine.setdefault(unit, []).extend(values)
        self.above_ref |= other.above_ref
        self.always |= other.always
        self.restrict |= other.restrict
        self.subjects |= other.subjects
        self.expiries += other.expiries


def _to_decimal(number: str, suffix: str | None) -> Decimal | None:
    try:
        value = Decimal(number.replace(",", ""))
    except InvalidOperation:  # pragma: no cover - the regex only admits digits
        return None
    if suffix:
        value *= MULTIPLIERS[suffix.lower()]
    return value


def _quantities(seg: str) -> list[_Quantity]:
    found: list[_Quantity] = []
    for rx, form in _MONEY_RES:
        for m in rx.finditer(seg):
            if form == "sym":
                unit, num, suf = CURRENCY_SYMBOLS[m.group(1)], m.group(2), m.group(3)
            elif form == "pre":
                unit, num, suf = CURRENCY_WORDS[m.group(1).lower()], m.group(2), m.group(3)
            else:
                num, suf, unit = m.group(1), m.group(2), CURRENCY_WORDS[m.group(3).lower()]
            value = _to_decimal(num, suf)
            if value is not None:
                found.append(_Quantity(m.start(), m.end(), value, unit))
    for m in _COUNT_RE.finditer(seg):
        value = _to_decimal(m.group(1), m.group(2))
        if value is not None:
            found.append(_Quantity(m.start(), m.end(), value, COUNT_UNITS[m.group(3).lower()]))
    # Overlapping matches (e.g. "$1,000 USD"): keep the earliest, longest.
    found.sort(key=lambda q: (q.start, -(q.end - q.start)))
    out: list[_Quantity] = []
    for q in found:
        if not out or q.start >= out[-1].end:
            out.append(q)
    return out


def find_quantities(text: str) -> list[tuple[Decimal, str]]:
    """Every amount or count in ``text`` as ``(value, unit)`` (used by Tier 1b)."""
    return [(q.value, q.unit) for q in _quantities(text)]


def mentions_approval(text: str) -> bool:
    return bool(_APPROVAL.search(text))


def negates_approval(text: str) -> bool:
    return bool(_NO_APPROVAL.search(text))


def _time_spans(seg: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _TIME_QTY_RE.finditer(seg)]


def _classify(seg: str, quantities: list[_Quantity], approval: bool) -> None:
    boundaries = sorted([(q.start, q.end) for q in quantities] + _time_spans(seg))
    for q in quantities:
        prev_end = max([e for s, e in boundaries if e <= q.start], default=0)
        left = seg[max(prev_end, q.start - 40) : q.start]
        best: tuple[int, int, str] | None = None  # (end, length, kind)
        for kind, rx in _CUE_RES:
            for m in rx.finditer(left):
                cand = (m.end(), m.end() - m.start(), kind)
                if best is None or cand[:2] > best[:2]:
                    best = cand
        if best is not None:
            kind = best[2]
            if kind == "above":
                if approval:
                    q.role = "above"
                elif _NEGATION.search(left):
                    q.role = "max"
            else:
                q.role = kind
            continue
        right = seg[q.end : q.end + 15]
        if _RIGHT_MAX.search(right):
            q.role = "max"
        elif _RIGHT_MIN.search(right):
            q.role = "min"


def _analyse_segment(
    seg: str, ctx: _Context, issued_at: datetime, *, order_is_verb: bool = False
) -> None:
    approval_text = _NO_APPROVAL.sub(" ", seg)
    approval = bool(_APPROVAL.search(approval_text))
    quantities = _quantities(seg)
    _classify(seg, quantities, approval)
    has_threshold = False
    for q in quantities:
        if q.role == "max":
            ctx.maxes.setdefault(q.unit, []).append(q.value)
        elif q.role == "min":
            ctx.mins.setdefault(q.unit, []).append(q.value)
        elif q.role == "above":
            ctx.thresholds.setdefault(q.unit, []).append(q.value)
            has_threshold = True
    if approval and not has_threshold:
        if _ABOVE_REF.search(approval_text):
            ctx.above_ref = True
        else:
            ctx.always = True
    if _RESTRICT.search(seg):
        ctx.restrict = True
    for m in _SUBJECT_RE.finditer(seg):
        if order_is_verb and m.start() == 0:
            continue  # "Order 20 laptops": the verb, not order #20
        kind = SUBJECT_KINDS[m.group(1).lower()]
        ident = m.group(2).lower().lstrip("-")
        ctx.subjects.add(f"{kind}:{ident}")
    for m in _PROJECT_RE.finditer(seg):
        ctx.subjects.add(f"project:{m.group(1).lower()}")
    for m in _REL_EXPIRY.finditer(seg):
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit.startswith("w"):
            delta = timedelta(weeks=n)
        elif unit.startswith("d"):
            delta = timedelta(days=n)
        elif unit.startswith("m"):
            delta = timedelta(minutes=n)
        else:
            delta = timedelta(hours=n)
        ctx.expiries.append(issued_at + delta)
    for m in _ABS_EXPIRY.finditer(seg):
        day = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=UTC)
        if m.group(2):
            hh, mm = (int(x) for x in m.group(2).split(":"))
            ctx.expiries.append(day.replace(hour=hh, minute=mm))
        else:
            ctx.expiries.append(day.replace(hour=23, minute=59, second=59))


def _action_mentions(sentence: str) -> tuple[list[tuple[int, str]], bool]:
    """Action mentions in a sentence, and whether "order" was read as the verb."""
    mentions = [
        (m.start(), action) for action, rx in _ACTION_RES.items() for m in rx.finditer(sentence)
    ]
    if not mentions:
        m = _ORDER_VERB.search(sentence)
        if m:
            return [(m.start(), "purchase")], True
    return sorted(mentions), False


def _contexts(text: str, issued_at: datetime) -> list[_Context]:
    contexts: list[_Context] = []
    pending = _Context(action="")  # attributes seen before any action
    for sentence in (s.strip() for s in _SENTENCE_SPLIT.split(text)):
        if not sentence:
            continue
        mentions, order_is_verb = _action_mentions(sentence)
        if not mentions:
            target = contexts[-1] if contexts else pending
            _analyse_segment(sentence, target, issued_at)
            continue
        bounds = [0] + [pos for pos, _ in mentions[1:]] + [len(sentence)]
        for (_, action), lo, hi in zip(mentions, bounds, bounds[1:], strict=False):
            if not contexts or contexts[-1].action != action:
                ctx = _Context(action=action)
                if not contexts:
                    ctx.absorb(pending)
                contexts.append(ctx)
            _analyse_segment(
                sentence[lo:hi], contexts[-1], issued_at, order_is_verb=order_is_verb and lo == 0
            )
    return contexts


def _specs_for(
    ctx: _Context, base: dict[str, Any]
) -> tuple[list[ConstraintSpec], list[ReviewFlag]]:
    flags: list[ReviewFlag] = []

    def pick(values: dict[str, list[Decimal]], fn: Any, label: str) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for unit, vs in values.items():
            if len(set(vs)) > 1:
                flags.append(
                    ReviewFlag(
                        kind="DUPLICATE_LIMIT",
                        action=ctx.action,
                        detail=f"several {label} values for {unit}: "
                        + ", ".join(str(v) for v in vs)
                        + "; the most restrictive was kept",
                    )
                )
            out[unit] = fn(vs)
        return out

    maxes = pick(ctx.maxes, min, "maximum")
    mins = pick(ctx.mins, max, "minimum")
    thresholds = pick(ctx.thresholds, min, "approval threshold")
    hard_auth: Authority = "requires_approval_always" if ctx.always else "none"

    common: dict[str, Any] = {
        **base,
        "action": ctx.action,
        "resource": ACTIONS[ctx.action][0],
        "subject": SubjectScope(frozenset(ctx.subjects)).format() if ctx.subjects else "*",
        "expiry": min(ctx.expiries) if ctx.expiries else None,
    }
    specs: list[ConstraintSpec] = []
    for unit in sorted(set(maxes) | set(mins) | set(thresholds)):
        mx, mn, th = maxes.get(unit), mins.get(unit), thresholds.get(unit)
        if th is None and mx is not None and ctx.above_ref:
            th, mx = mx, None
        if th is not None and mx is not None and mx != th:
            specs.append(
                ConstraintSpec(
                    **common,
                    limit=Limit(max_value=mx, min_value=mn, unit=unit),
                    authority=hard_auth,
                )
            )
            mn = None
        if th is not None:
            specs.append(
                ConstraintSpec(
                    **common,
                    limit=Limit(max_value=th, min_value=mn, unit=unit),
                    authority="requires_approval_above_limit",
                )
            )
        elif mx is not None or mn is not None:
            specs.append(
                ConstraintSpec(
                    **common,
                    limit=Limit(max_value=mx, min_value=mn, unit=unit),
                    authority=hard_auth,
                )
            )
    if not specs:
        if ctx.always or ctx.above_ref:
            specs.append(ConstraintSpec(**common, authority="requires_approval_always"))
        elif common["expiry"] is not None or (ctx.restrict and ctx.subjects):
            specs.append(ConstraintSpec(**common))
    return specs, flags


def _base_fields(context: ExtractionContext) -> dict[str, Any]:
    return {
        "scope": context.scope,
        "owner": context.owner,
        "origin": context.origin,
        "risk_class": context.risk_class,
        "max_age_seconds": context.max_age_seconds,
    }


def extract_rules(
    instruction: str, context: ExtractionContext
) -> tuple[list[ConstraintSpec], list[ReviewFlag]]:
    """Deterministic rule-based extraction (§7.1 step 1)."""
    contexts = _contexts(instruction, context.issued_at)
    base = _base_fields(context)
    specs: list[ConstraintSpec] = []
    flags: list[ReviewFlag] = []
    for ctx in contexts:
        s, f = _specs_for(ctx, base)
        specs += s
        flags += f
    return specs, flags


# --------------------------------------------------------------------------- LLM path


class _LLMLimit(_Frozen):
    max_value: Decimal | None = None
    min_value: Decimal | None = None
    unit: str = Field(min_length=1)


class _LLMConstraint(_Frozen):
    action: str = Field(min_length=1)
    resource: str = Field(min_length=1)
    subject: str = "*"
    limit: _LLMLimit | None = None
    authority: Authority = "none"
    expiry: AwareDatetime | None = None


class _LLMOutput(_Frozen):
    model_config = ConfigDict(extra="forbid", frozen=True)
    constraints: list[_LLMConstraint]


def parse_llm_output(raw: str, context: ExtractionContext) -> list[ConstraintSpec]:
    """Validate the LLM's JSON against the schema; raises ``ValueError`` when invalid."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"output is not valid JSON: {exc.msg}") from exc
    try:
        parsed = _LLMOutput.model_validate(data)
        base = _base_fields(context)
        specs = []
        for c in parsed.constraints:
            if c.expiry is not None and c.expiry <= context.issued_at:
                raise ValueError(f"expiry {c.expiry.isoformat()} is not after the issue time")
            specs.append(
                ConstraintSpec(
                    **base,
                    action=c.action.lower(),
                    resource=c.resource.lower(),
                    subject=c.subject,
                    limit=(
                        None
                        if c.limit is None
                        else Limit(
                            max_value=c.limit.max_value,
                            min_value=c.limit.min_value,
                            unit=c.limit.unit,
                        )
                    ),
                    authority=c.authority,
                    expiry=c.expiry,
                )
            )
        return specs
    except ValidationError as exc:
        raise ValueError(_short_errors(exc)) from exc


def _short_errors(exc: ValidationError) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()]
    return "schema validation failed: " + "; ".join(parts[:5])


async def extract_llm(
    instruction: str, context: ExtractionContext, judge: Judge
) -> tuple[list[ConstraintSpec] | None, list[ReviewFlag]]:
    """LLM extraction with one repair retry (§7.1 step 2).

    Returns ``(None, flags)`` when the judge is unavailable or the output is rejected.
    """
    hint: str | None = None
    for _attempt in range(2):
        try:
            raw = await judge.extract_constraints(
                instruction=instruction, issued_at=context.issued_at, repair_hint=hint
            )
        except DependencyUnavailable as exc:
            return None, [ReviewFlag(kind="LLM_UNAVAILABLE", detail=str(exc))]
        try:
            return parse_llm_output(raw, context), []
        except ValueError as exc:
            hint = str(exc)
    return None, [ReviewFlag(kind="LLM_REJECTED", detail=f"rejected after one repair: {hint}")]


# --------------------------------------------------------------------------- reconcile

_AUTH_STRICTNESS = {"requires_approval_above_limit": 0, "none": 1, "requires_approval_always": 2}


def _min_opt(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return b if a is None else a if b is None else min(a, b)


def _max_opt(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return b if a is None else a if b is None else max(a, b)


def _reconcile_pair(
    r: ConstraintSpec, m: ConstraintSpec
) -> tuple[ConstraintSpec, list[ReviewFlag]]:
    flags: list[ReviewFlag] = []
    a = r.action
    if r.limit and m.limit and r.limit.unit != m.limit.unit:
        flags.append(
            ReviewFlag(
                kind="UNIT_DISAGREEMENT",
                action=a,
                detail=f"rule unit {r.limit.unit} vs LLM unit {m.limit.unit}; rule value kept",
            )
        )
        return r, flags

    limit = r.limit or m.limit
    if r.limit and m.limit:
        mx = _min_opt(r.limit.max_value, m.limit.max_value)
        mn = _max_opt(r.limit.min_value, m.limit.min_value)
        if (r.limit.max_value, r.limit.min_value) != (m.limit.max_value, m.limit.min_value):
            flags.append(
                ReviewFlag(
                    kind="LIMIT_DISAGREEMENT",
                    action=a,
                    detail=f"rule {r.limit.min_value}..{r.limit.max_value} vs LLM "
                    f"{m.limit.min_value}..{m.limit.max_value} {r.limit.unit}; "
                    "kept the more restrictive",
                )
            )
        if mn is not None and mx is not None and mn > mx:
            mn = None  # resolving each bound independently must not create an empty range
        limit = Limit(max_value=mx, min_value=mn, unit=r.limit.unit)
    elif r.limit != m.limit:
        flags.append(
            ReviewFlag(kind="LIMIT_DISAGREEMENT", action=a, detail="only one path found a limit")
        )

    authority = max(r.authority, m.authority, key=lambda x: _AUTH_STRICTNESS[x])
    if authority == "requires_approval_above_limit" and (limit is None or limit.max_value is None):
        authority = "requires_approval_always"
    subject = SubjectScope.parse(r.subject).intersect(SubjectScope.parse(m.subject))
    expiry = min((e for e in (r.expiry, m.expiry) if e is not None), default=None)
    other = []
    if r.authority != m.authority:
        other.append(f"authority {r.authority} vs {m.authority}")
    if SubjectScope.parse(r.subject) != SubjectScope.parse(m.subject):
        other.append(f"subject {r.subject} vs {m.subject}")
    if r.expiry != m.expiry:
        other.append("expiry differs")
    if other:
        flags.append(
            ReviewFlag(
                kind="FIELD_DISAGREEMENT",
                action=a,
                detail="; ".join(other) + "; kept the more restrictive",
            )
        )
    if subject.is_empty:
        flags.append(
            ReviewFlag(
                kind="FIELD_DISAGREEMENT",
                action=a,
                detail="subjects do not overlap; rule subject kept",
            )
        )
        subject = SubjectScope.parse(r.subject)
    merged = r.model_copy(
        update={
            "limit": limit,
            "authority": authority,
            "subject": subject.format(),
            "expiry": expiry,
        }
    )
    return ConstraintSpec.model_validate(merged.model_dump()), flags


def reconcile(
    rule_specs: list[ConstraintSpec], llm_specs: list[ConstraintSpec]
) -> tuple[list[ConstraintSpec], list[Source], list[ReviewFlag]]:
    """Merge both paths (§7.1 step 3). One-to-one matches per (action, resource) are
    merged most-restrictively; anything else is kept from both sides (the union is
    never looser, because composition takes the strictest of all registered constraints).
    """
    keys = sorted({(s.action, s.resource) for s in rule_specs + llm_specs})
    specs: list[ConstraintSpec] = []
    sources: list[Source] = []
    flags: list[ReviewFlag] = []
    for key in keys:
        rs = [s for s in rule_specs if (s.action, s.resource) == key]
        ms = [s for s in llm_specs if (s.action, s.resource) == key]
        if len(rs) == 1 and len(ms) == 1:
            merged, f = _reconcile_pair(rs[0], ms[0])
            specs.append(merged)
            sources.append("both")
            flags += f
            continue
        if rs and ms:
            flags.append(
                ReviewFlag(
                    kind="AMBIGUOUS_MATCH",
                    action=key[0],
                    detail=f"{len(rs)} rule vs {len(ms)} LLM constraints; all kept",
                )
            )
        specs += rs + ms
        rule_src: list[Source] = ["rule"] * len(rs)
        llm_src: list[Source] = ["llm"] * len(ms)
        sources += rule_src + llm_src
    return specs, sources, flags


# --------------------------------------------------------------------------- facade


async def extract(
    instruction: str,
    context: ExtractionContext,
    *,
    judge: Judge | None = None,
    llm_mode: LLMMode = "fallback",
) -> ExtractionResult:
    """Run the extraction pipeline (§7.1).

    ``llm_mode``: ``off`` = rules only; ``fallback`` = call the LLM only when rules
    find nothing; ``always`` = call it every time and reconcile (enrichment).
    """
    rule_specs, flags = extract_rules(instruction, context)
    want_llm = judge is not None and (
        llm_mode == "always" or (llm_mode == "fallback" and not rule_specs)
    )
    if not want_llm or judge is None:
        return ExtractionResult(specs=rule_specs, sources=["rule"] * len(rule_specs), flags=flags)

    llm_specs, llm_flags = await extract_llm(instruction, context, judge)
    flags += llm_flags
    if llm_specs is None:
        return ExtractionResult(
            specs=rule_specs,
            sources=["rule"] * len(rule_specs),
            flags=flags,
            llm_called=True,
            degraded=True,
        )
    specs, sources, rec_flags = reconcile(rule_specs, llm_specs)
    return ExtractionResult(specs=specs, sources=sources, flags=flags + rec_flags, llm_called=True)
