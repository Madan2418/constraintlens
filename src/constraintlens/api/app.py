"""FastAPI application (ARCHITECTURE.md §11). Wiring lives in ``api/wiring.py``.

Run locally::

    uv run python -m constraintlens.api          # honours CL_HOST / CL_PORT / CL_DB_URL
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request

from constraintlens import __version__
from constraintlens.api import errors
from constraintlens.api.routes import system, traces
from constraintlens.api.wiring import Container, default_container
from constraintlens.config import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from fastapi.responses import Response


def create_app(settings: Settings | None = None, *, container: Container | None = None) -> FastAPI:
    settings = settings or (container.settings if container else Settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container or await default_container(settings)
        yield
        close = getattr(app.state.container.store, "close", None)
        if close is not None and container is None:
            await close()

    app = FastAPI(
        title="ConstraintLens",
        version=__version__,
        summary="Runtime detection of semantic constraint drift across agent hops.",
        lifespan=lifespan,
    )
    if container is not None:
        app.state.container = container  # usable without running the lifespan
    errors.install(app)

    @app.middleware("http")
    async def limit_body(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        length = request.headers.get("content-length")
        if length is not None and length.isdigit() and int(length) > settings.max_request_bytes:
            return errors.problem(
                413, "Request too large", f"limit is {settings.max_request_bytes} bytes"
            )
        return await call_next(request)

    app.include_router(system.router)
    app.include_router(traces.router)
    return app


def main() -> None:  # pragma: no cover - thin process entry point
    import uvicorn

    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
