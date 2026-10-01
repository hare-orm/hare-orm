"""A transaction around a request - like Django's ``ATOMIC_REQUESTS`` - for every framework: it
commits when the response starts with a status below 500 and rolls back on a server error response
or an exception, handled or not."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from hare.contrib.frameworks.constants import (
    REQUEST_TRANSACTION_SCOPE_KEY,
    SERVER_ERROR_STATUS_CODE,
    SKIP_TRANSACTION_ATTRIBUTE,
)
from hare.contrib.frameworks.exceptions import ServerErrorResponse
from hare.transactions.transactions import Transactions

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.transactions.atomic import Atomic


class RequestTransaction:
    """The transactions of one request, one per connection. With several connections they commit
    one after another - a failure to commit one rolls back those not yet committed, not those
    already committed.

    Args:
        connection_names: The connections, None for the default one.
    """

    def __init__(self, connection_names: tuple[str | None, ...]) -> None:
        self.connection_names = connection_names
        self.open_contexts: list[Atomic] = []
        #: An exception the handler raised and the application answered itself.
        self.handled_error: BaseException | None = None

    @staticmethod
    def fail_request(scope: Any, error: BaseException) -> None:
        """Rolls back the transaction of a request whose handler raised - for the framework's hook
        that runs as the application answers the exception itself (``404`` for ``DoesNotExist``).

        Args:
            scope: The request's ASGI scope.
            error: The exception.
        """
        request_transaction = scope.get(REQUEST_TRANSACTION_SCOPE_KEY)
        if request_transaction is not None:
            request_transaction.handled_error = error

    @staticmethod
    def skip[HandlerType: Callable[..., Any]](handler: HandlerType) -> HandlerType:
        """Marks a route handler that runs outside the request's transaction::

            @app.post("/import")
            @RequestTransaction.skip
            async def import_rows(...): ...

        Args:
            handler: The route handler.

        Returns:
            The handler itself.
        """
        setattr(handler, SKIP_TRANSACTION_ATTRIBUTE, True)
        return handler

    @staticmethod
    def is_skipped(handler: Any) -> bool:
        """Whether a route handler runs outside the request's transaction.

        Args:
            handler: The route handler, or None for a route without one.

        Returns:
            True for a handler marked with ``skip()``.
        """
        return getattr(handler, SKIP_TRANSACTION_ATTRIBUTE, False) is True

    async def run[ResponseType](
        self, call: Callable[[], Awaitable[ResponseType]], get_status_code: Callable[[ResponseType], int]
    ) -> ResponseType:
        """Runs a handler in the transactions and ends them by its response - before the
        response is sent, so a failed commit is still answered with an error.

        Args:
            call: Runs the handler and returns its response.
            get_status_code: The status code of a response.

        Returns:
            The response.

        Raises:
            Exception: The handler raised - the transactions are rolled back - or a commit failed.
        """
        await self.begin()
        try:
            response = await call()
        except BaseException as error:
            await self.close(error)
            raise
        await self.finish(get_status_code(response))
        return response

    async def run_asgi(self, app: Any, scope: Any, receive: Any, send: Any) -> None:
        """Runs an ASGI application in the transactions, ending them as its response starts - a
        streaming response's body is sent after the commit.

        Args:
            app: The ASGI application.
            scope: The request's scope.
            receive: The ASGI receive callable.
            send: The ASGI send callable.

        Raises:
            Exception: The application raised - the transactions are rolled back - or a commit
                failed.
        """
        await self.begin()
        scope[REQUEST_TRANSACTION_SCOPE_KEY] = self

        async def send_after_commit(message: Any) -> None:
            if message["type"] == "http.response.start":
                await self.finish(message["status"])
            await send(message)

        try:
            await app(scope, receive, send_after_commit)
        except BaseException as error:
            await self.close(error)
            raise
        # An application that never started a response (a closed connection) leaves nothing to
        # commit.
        await self.close(ServerErrorResponse(SERVER_ERROR_STATUS_CODE))

    async def begin(self) -> None:
        """Opens a transaction on each connection.

        Raises:
            Exception: Opening one failed - those already opened are rolled back.
        """
        for connection_name in self.connection_names:
            context = Transactions.atomic(connection_name)
            try:
                await context.__aenter__()
            except BaseException as error:
                await self.close(error)
                raise
            self.open_contexts.append(context)

    async def finish(self, status_code: int) -> None:
        """Ends the transactions as the response starts: commits them, or rolls them back for a
        server error or an exception the application answered itself.

        Args:
            status_code: The response's status code.

        Raises:
            Exception: A commit failed - the transactions not committed yet are rolled back.
        """
        error = self.handled_error
        if error is None and status_code >= SERVER_ERROR_STATUS_CODE:
            error = ServerErrorResponse(status_code)
        await self.close(error)

    async def close(self, error: BaseException | None) -> None:
        """Ends the transactions still open, the last opened first - does nothing once they ended.

        Args:
            error: Why to roll back, None to commit.

        Raises:
            Exception: A commit failed, when committing.
        """
        contexts, self.open_contexts = self.open_contexts, []
        failure: BaseException | None = None
        for context in reversed(contexts):
            reason = error or failure
            try:
                if reason is None:
                    await context.__aexit__(None, None, None)
                else:
                    await context.__aexit__(type(reason), reason, reason.__traceback__)
            except Exception as close_error:
                failure = failure or close_error
        if failure is not None and error is None:
            raise failure
