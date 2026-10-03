from __future__ import annotations

from typing import TYPE_CHECKING

from litestar.enums import ScopeType
from litestar.middleware import ASGIMiddleware

from hare.contrib.frameworks.litestar.constants import SKIP_TRANSACTION_OPT_KEY
from hare.contrib.frameworks.transactions import RequestTransaction

if TYPE_CHECKING:  # pragma: nocoverage
    from litestar.types import ASGIApp, Receive, Scope, Send


class TransactionMiddleware(ASGIMiddleware):
    """Runs each HTTP request in a transaction on each of the connections, like Django's
    ``ATOMIC_REQUESTS``: it commits when the response starts with a status below 500 and rolls
    back on a server error response or an exception. A streaming response's body is sent after the
    commit. A route handler with ``opt={SKIP_TRANSACTION_OPT_KEY: True}`` runs without it.

    With several connections, the transactions commit one after another - a failure to commit one
    rolls back those not yet committed, not those already committed.

    Args:
        connection_names: The connections, None for the default one.
    """

    scopes = (ScopeType.HTTP,)
    exclude_opt_key = SKIP_TRANSACTION_OPT_KEY

    def __init__(self, connection_names: tuple[str | None, ...]) -> None:
        self.connection_names = connection_names

    async def handle(self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp) -> None:
        await RequestTransaction(self.connection_names).run_asgi(next_app, scope, receive, send)
