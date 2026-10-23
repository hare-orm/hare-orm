from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from starlette.routing import Match

from hare.contrib.frameworks.request_transaction import RequestTransaction

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


class TransactionMiddleware:
    """Runs each HTTP request in a transaction on each of the connections, like Django's
    ``ATOMIC_REQUESTS`` - see ``RequestTransaction``. A route whose endpoint is marked with
    ``RequestTransaction.skip`` runs without it.

    Args:
        app: The application.
        connection_aliases: The connections, None for the default one.
    """

    def __init__(self, app: ASGIApp, connection_aliases: tuple[str | None, ...]) -> None:
        self.app = app
        self.connection_aliases = connection_aliases

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.is_skipped(scope):
            await self.app(scope, receive, send)
            return
        await RequestTransaction(self.connection_aliases).run_asgi(self.app, scope, receive, send)

    @classmethod
    def is_skipped(cls, scope: Scope) -> bool:
        """Whether the route a request goes to runs outside the request's transaction.

        Args:
            scope: The request's scope.

        Returns:
            True when the application's route matching the request has an endpoint marked with
            ``RequestTransaction.skip`` - one of an included router or a mounted application too.
        """
        return cls.is_route_skipped(scope["app"].routes, scope)

    @classmethod
    def is_route_skipped(cls, routes: Iterable[Any], scope: Scope) -> bool:
        """Whether the route of ``routes`` a request goes to runs outside the request's transaction -
        looked for in the routes of a router or an application the matching route holds.

        Args:
            routes: The routes.
            scope: The request's scope, as the routes match it.

        Returns:
            True when the matching endpoint is marked with ``RequestTransaction.skip``.
        """
        for route in routes:
            match, child_scope = route.matches(scope)
            if match is not Match.FULL:
                continue
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None:
                return RequestTransaction.is_skipped(endpoint)
            # An included router's routes, or the routes of a mounted application.
            candidates = getattr(route, "effective_candidates", None)
            nested_routes = candidates() if callable(candidates) else getattr(route, "routes", None)
            if not nested_routes:
                return False
            return cls.is_route_skipped(nested_routes, {**scope, **child_scope})
        return False
