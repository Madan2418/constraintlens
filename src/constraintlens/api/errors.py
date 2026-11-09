"""RFC 7807 problem-details responses (ARCHITECTURE.md §11.4)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from constraintlens.domain.errors import ConstraintLensError

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

PROBLEM_JSON = "application/problem+json"


def problem(
    status: int, title: str, detail: str = "", *, type_: str = "about:blank", **extra: Any
) -> JSONResponse:
    body = {"type": type_, "title": title, "status": status, "detail": detail, **extra}
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


def install(app: FastAPI) -> None:
    @app.exception_handler(ConstraintLensError)
    async def _domain(_: Request, exc: ConstraintLensError) -> JSONResponse:
        return problem(
            exc.status_code,
            exc.title,
            str(exc),
            type_=f"urn:constraintlens:error:{type(exc).__name__}",
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Malformed constraints and envelopes are rejected at the boundary (§5): 400.
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        return problem(
            400,
            "Invalid envelope",
            "request failed validation",
            type_="urn:constraintlens:error:InvalidEnvelope",
            errors=errors[:20],
        )
