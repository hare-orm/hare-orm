from __future__ import annotations

from typing import TYPE_CHECKING

from hare.contrib.frameworks.fastapi.constants import REQUEST_SCOPE_TYPES
from hare.contrib.frameworks.request_state import RequestState

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from hare.contrib.application_lifecycle import ApplicationLifecycle


class RequestStateResetMiddleware:
    """Starts each request with its own state (``RequestState``): ``RepeatedQueryDetector`` counts
    the queries of one request, and only the request's own writes send its reads to the connection
    written through. The application's lifecycle scope is active for the request without owning
    or closing that lifecycle.

    Args:
        app: The application.
        lifecycle: The application's active Hare lifecycle.
    """

    def __init__(self, app: ASGIApp, lifecycle: ApplicationLifecycle) -> None:
        self.app = app
        self.lifecycle = lifecycle

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in REQUEST_SCOPE_TYPES:
            with self.lifecycle.scope():
                RequestState.reset()
                await self.app(scope, receive, send)
            return
        await self.app(scope, receive, send)
