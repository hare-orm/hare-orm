from __future__ import annotations

from typing import TYPE_CHECKING

from litestar.enums import ScopeType
from litestar.middleware import ASGIMiddleware

from hare.contrib.frameworks.request_state import RequestState

if TYPE_CHECKING:
    from litestar.types import ASGIApp, Receive, Scope, Send

    from hare.contrib.application_lifecycle import ApplicationLifecycle


class RequestStateResetMiddleware(ASGIMiddleware):
    """Starts each request with its own state (``RequestState``): ``RepeatedQueryDetector`` counts
    the queries of one request, and only the request's own writes send its reads to the connection
    written through. The application's lifecycle scope is active for the request without owning
    or closing that lifecycle.

    Args:
        lifecycle: The application's active Hare lifecycle.
    """

    scopes = (ScopeType.HTTP, ScopeType.WEBSOCKET)

    def __init__(self, lifecycle: ApplicationLifecycle) -> None:
        self.lifecycle = lifecycle

    async def handle(self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp) -> None:
        with self.lifecycle.scope():
            RequestState.reset()
            await next_app(scope, receive, send)
