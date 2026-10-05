from __future__ import annotations

from typing import TYPE_CHECKING

from hare.contrib.frameworks.fastapi.constants import REQUEST_SCOPE_TYPES
from hare.contrib.frameworks.request_state import RequestState

if TYPE_CHECKING:  # pragma: nocoverage
    from starlette.types import ASGIApp, Receive, Scope, Send


class RequestStateResetMiddleware:
    """Starts each request with its own state (``RequestState``): ``RepeatedQueryDetector`` counts
    the queries of one request, and only the request's own writes send its reads to the connection
    written through.

    Args:
        app: The application.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in REQUEST_SCOPE_TYPES:
            RequestState.reset()
        await self.app(scope, receive, send)
