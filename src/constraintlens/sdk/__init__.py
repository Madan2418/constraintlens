"""Client SDK used by pipelines (ARCHITECTURE.md §7.3)."""

from constraintlens.domain.models import ActionBinding
from constraintlens.sdk.client import (
    CircuitBreaker,
    ConstraintLens,
    ConstraintLensClientError,
    ConstraintViolation,
    HopContext,
    OpenedTrace,
    ServiceUnavailable,
    current_hop,
    next_envelope,
)

__all__ = [
    "ActionBinding",
    "CircuitBreaker",
    "ConstraintLens",
    "ConstraintLensClientError",
    "ConstraintViolation",
    "HopContext",
    "OpenedTrace",
    "ServiceUnavailable",
    "current_hop",
    "next_envelope",
]
