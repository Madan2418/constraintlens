"""Domain errors (ARCHITECTURE.md §11.4).

The delivery layer maps each to an RFC 7807 problem response; the HTTP status
is recorded here as data so the mapping cannot drift from the spec.
"""

from __future__ import annotations


class ConstraintLensError(Exception):
    """Base class for all domain errors."""

    status_code: int = 500
    title: str = "ConstraintLens error"


class ConstraintViolation(ConstraintLensError):
    """A checkpoint was blocked; at TOOL_CALL the tool must not execute."""

    status_code = 422
    title = "Constraint violation"


class InvalidEnvelope(ConstraintLensError):
    status_code = 400
    title = "Invalid envelope"


class UnknownTrace(ConstraintLensError):
    status_code = 404
    title = "Unknown trace"


class UnsatisfiableConstraints(ConstraintLensError):
    status_code = 409
    title = "Unsatisfiable constraints"


class DependencyUnavailable(ConstraintLensError):
    """A port's backing dependency (judge, store, model, transport) failed or timed out."""

    status_code = 503
    title = "Dependency unavailable"

    def __init__(self, dependency: str, detail: str = "") -> None:
        self.dependency = dependency
        self.detail = detail
        super().__init__(f"{dependency} unavailable" + (f": {detail}" if detail else ""))
