from __future__ import annotations

from typing import TYPE_CHECKING

from litestar.enums import ScopeType
from litestar.middleware import ASGIMiddleware

from hare.contrib.repeated_queries import RepeatedQueryDetector

if TYPE_CHECKING:  # pragma: nocoverage
    from litestar.types import ASGIApp, Receive, Scope, Send


class RepeatedQueryResetMiddleware(ASGIMiddleware):
    """Starts each request with an empty count of repeated queries, so
    ``RepeatedQueryDetector`` counts the queries of one request, not of every request its task
    served before."""

    scopes = (ScopeType.HTTP, ScopeType.WEBSOCKET)

    async def handle(self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp) -> None:
        RepeatedQueryDetector.reset_counts()
        await next_app(scope, receive, send)
