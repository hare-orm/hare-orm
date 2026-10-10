from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.routing import Match

from hare.contrib.frameworks.request_transaction import RequestTransaction

if TYPE_CHECKING:  # pragma: nocoverage
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

    @staticmethod
    def is_skipped(scope: Scope) -> bool:
        """Whether the route a request goes to runs outside the request's transaction.

        Args:
            scope: The request's scope.

        Returns:
            True when the application's route matching the request has an endpoint marked with
            ``RequestTransaction.skip``.
        """
        for route in scope["app"].routes:
            match, _ = route.matches(scope)
            if match is Match.FULL:
                return RequestTransaction.is_skipped(getattr(route, "endpoint", None))
        return False
