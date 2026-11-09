"""Request dependencies: the container and API-key authentication (§21.1)."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, Request

from constraintlens.api.wiring import Container
from constraintlens.domain.errors import ConstraintLensError


class Unauthorized(ConstraintLensError):
    status_code = 401
    title = "Unauthorized"


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


def require_api_key(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> None:
    expected = get_container(request).settings.api_key
    if expected is None or not expected.get_secret_value():
        return  # authentication disabled (local development)
    if x_api_key is None or not hmac.compare_digest(
        x_api_key.encode(), expected.get_secret_value().encode()
    ):
        raise Unauthorized("missing or invalid X-API-Key header")


ContainerDep = Annotated[Container, Depends(get_container)]
Authenticated = Depends(require_api_key)
