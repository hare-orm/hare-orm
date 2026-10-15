from __future__ import annotations

from typing import TYPE_CHECKING

from litestar.enums import ScopeType
from litestar.middleware import ASGIMiddleware

from hare.contrib.frameworks.request_state import RequestState

if TYPE_CHECKING:  # pragma: nocoverage
    from litestar.types import ASGIApp, Receive, Scope, Send


class RequestStateResetMiddleware(ASGIMiddleware):
    """Starts each request with its own state (``RequestState``): ``RepeatedQueryDetector`` counts
    the queries of one request, and only the request's own writes send its reads to the connection
    written through."""

    scopes = (ScopeType.HTTP, ScopeType.WEBSOCKET)

    async def handle(self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp) -> None:
        RequestState.reset()
        await next_app(scope, receive, send)
