from __future__ import annotations

from typing import TYPE_CHECKING

from hare.contrib.repeated_queries import RepeatedQueryDetector

if TYPE_CHECKING:  # pragma: nocoverage
    from starlette.types import ASGIApp, Receive, Scope, Send


#: The ASGI scopes a request's count of repeated queries starts empty for.
REQUEST_SCOPE_TYPES = frozenset({"http", "websocket"})


class RepeatedQueryResetMiddleware:
    """Starts each request with an empty count of repeated queries, so
    ``RepeatedQueryDetector`` counts the queries of one request, not of every request its task
    served before.

    Args:
        app: The application.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in REQUEST_SCOPE_TYPES:
            RepeatedQueryDetector.reset_counts()
        await self.app(scope, receive, send)
