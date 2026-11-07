"""Canonicalization and hashing (ARCHITECTURE.md §7.1 step 4, §7.2).

* ``canonical_text``: a deterministic plain-language restatement of a constraint.
  It is the hypothesis NLI and the judge check task text against (§7.4), and the
  text returned on RE_GROUND (§7.8).
* ``content_hash``: SHA-256 over the constraint's canonical JSON (every field
  except the hash itself). Tier 0 recomputes it to detect tampering (§7.2).
* ``envelope_digest``: SHA-256 over the trace id plus each constraint's id and hash.

Canonical JSON uses sorted keys, no whitespace, decimals without exponent or trailing
zeros, UUIDs in lower case, and datetimes in UTC with microseconds and a ``Z`` suffix.
So two constraints that mean the same thing hash the same, however they were built.

Pure module: standard library and domain only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from constraintlens.domain.models import Constraint, ConstraintSpec, Limit
from constraintlens.domain.subject import SubjectScope

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

HASH_EXCLUDED_FIELDS = frozenset({"content_hash"})


# --------------------------------------------------------------------------- primitives


def canonical_decimal(value: Decimal) -> str:
    """``Decimal("100.00")`` → ``"100"``, ``Decimal("1E+3")`` → ``"1000"``, ``0.50`` → ``"0.5"``."""
    if not value.is_finite():
        raise ValueError(f"non-finite decimal {value}")
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", "0"} else text


def canonical_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("canonical datetimes must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, datetime):
        return canonical_datetime(value)
    if isinstance(value, dict):
        return {str(k): _canonical_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical_value(v) for v in value]
    if value is None or isinstance(value, bool | int | str):
        return value
    return str(value)  # UUID and other scalar value objects


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- text


def _fmt_amount(value: Decimal, unit: str) -> str:
    return f"{canonical_decimal(value)} {unit}"


def render_canonical_text(spec: ConstraintSpec) -> str:
    """Deterministic plain-language restatement of a constraint.

    Example: ``refund (order) for order:4471: amount must not exceed 100 USD;
    any amount above 100 USD requires approval.``
    """
    subject = SubjectScope.parse(spec.subject)
    head = f"{spec.action} ({spec.resource})"
    if not subject.is_universe:
        head += f" for {subject.format()}"
        if spec.limit is None and spec.authority == "none":
            head += " only"

    clauses: list[str] = []
    lim = spec.limit
    if lim is not None:
        if lim.max_value is not None and lim.min_value is not None:
            clauses.append(
                f"amount must be between {_fmt_amount(lim.min_value, lim.unit)} "
                f"and {_fmt_amount(lim.max_value, lim.unit)}"
            )
        elif lim.max_value is not None:
            clauses.append(f"amount must not exceed {_fmt_amount(lim.max_value, lim.unit)}")
        elif lim.min_value is not None:
            clauses.append(f"amount must be at least {_fmt_amount(lim.min_value, lim.unit)}")

    if spec.authority == "requires_approval_above_limit":
        assert lim is not None
        assert lim.max_value is not None
        clauses.append(f"any amount above {_fmt_amount(lim.max_value, lim.unit)} requires approval")
    elif spec.authority == "requires_approval_always":
        clauses.append("approval is required before execution")

    if spec.expiry is not None:
        clauses.append(f"valid until {canonical_datetime(spec.expiry)}")

    if not clauses:
        return f"{head}."
    return f"{head}: {'; '.join(clauses)}."


# --------------------------------------------------------------------------- hashing


def _hash_payload(constraint_fields: dict[str, Any]) -> str:
    body = {k: v for k, v in constraint_fields.items() if k not in HASH_EXCLUDED_FIELDS}
    return sha256_hex(canonical_json(body))


def compute_content_hash(constraint: Constraint) -> str:
    return _hash_payload(constraint.model_dump())


def verify_content_hash(constraint: Constraint) -> bool:
    """Tier 0 check: does the stored hash match the constraint's content? (§7.2)"""
    return compute_content_hash(constraint) == constraint.content_hash


def issue(spec: ConstraintSpec, *, constraint_id: UUID, issued_at: datetime) -> Constraint:
    """Turn a spec into an issued, canonicalized, hashed ``Constraint``.

    Values are normalised first (decimal form, UTC times, canonical subject), so
    equal meanings give equal hashes.
    """
    normalised = spec.model_copy(
        update={
            "subject": SubjectScope.parse(spec.subject).format(),
            "limit": _normalise_limit(spec.limit),
            "expiry": spec.expiry.astimezone(UTC) if spec.expiry else None,
        }
    )
    fields: dict[str, Any] = {
        **normalised.model_dump(),
        "constraint_id": constraint_id,
        "issued_at": issued_at.astimezone(UTC),
        "canonical_text": render_canonical_text(normalised),
    }
    fields["content_hash"] = _hash_payload(fields)
    return Constraint.model_validate(fields)


def _normalise_limit(limit: Limit | None) -> Limit | None:
    if limit is None:
        return None
    return Limit(
        max_value=None if limit.max_value is None else Decimal(canonical_decimal(limit.max_value)),
        min_value=None if limit.min_value is None else Decimal(canonical_decimal(limit.min_value)),
        unit=limit.unit,
    )


def compute_envelope_digest(trace_id: UUID, constraints: Iterable[Constraint]) -> str:
    """Hash over trace identity plus each constraint's id and content hash (§7.2)."""
    pairs = sorted((str(c.constraint_id), c.content_hash) for c in constraints)
    return sha256_hex(canonical_json({"trace_id": str(trace_id), "constraints": pairs}))
