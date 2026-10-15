from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial, wraps
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from hare.core.connections.connection_handler import ConnectionHandler
from hare.core.connections.connections import Connections
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.core.hare_context import HareContext
from hare.exceptions import QueryError, TransactionManagementError, UnSupportedError
from hare.transactions.atomic.atomic_attempts import AtomicAttempts
from hare.transactions.transaction_options import TransactionOptions

if TYPE_CHECKING:  # pragma: nocoverage
    from types import TracebackType

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.dialects.base.transactions.contexts.transaction_context import TransactionContext

T = TypeVar("T")
P = ParamSpec("P")


class Atomic:
    """One transaction on a connection: ``async with`` runs its block in the transaction, and as a
    decorator it runs every call of the function in a transaction of its own.

    Nested inside another transaction of the same connection it is a savepoint.

    With ``retries`` it runs again after a ``TransactionRetryError``: the decorated function is
    called again, and a block is run by ``async for attempt in atomic(retries=N): async with attempt:``.

    Args:
        get_connection: Gives the connection the transaction runs on, when it starts.
        options: How the transaction runs.
        retries: How many more times a run ending with ``TransactionRetryError`` is followed by another.
    """

    __slots__ = ("_get_connection", "_options", "_context", "_retries")

    def __init__(
        self, get_connection: Callable[[], DatabaseClient], options: TransactionOptions, retries: int = 0
    ) -> None:
        self._get_connection = get_connection
        self._options = options
        self._context: TransactionContext | None = None
        self._retries = retries

    def __aiter__(self) -> AtomicAttempts:
        return AtomicAttempts(self._get_connection, self._options, self._retries)

    async def __aenter__(self) -> TransactionClient:
        if self._retries:
            raise QueryError(
                "atomic(retries=...) runs a block again as `async for attempt in atomic(retries=...): "
                "async with attempt: ...` - a plain `async with` block can't run twice"
            )
        if self._context is not None:
            raise TransactionManagementError("This transaction was already used - create a new one with atomic()")
        connection = self._get_connection()
        if not connection.features.supports_transactions:
            raise UnSupportedError(
                f"{connection.connection_alias!r} is a {connection.dialect} database, which has no transactions"
            )
        if self._options.isolation is not None:
            connection.dialect.transactions.get_isolation_level(self._options.isolation)
        self._context = connection._in_transaction(self._options)
        return await self._context.__aenter__()

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        if self._context is None:
            raise TransactionManagementError("This transaction was never started")
        await self._context.__aexit__(exception_type, exception, exception_traceback)

    @classmethod
    def create(cls, using: str | None = None, options: TransactionOptions | None = None) -> Atomic:
        """A transaction on a connection chosen by name.

        Args:
            using: The connection's name, optional with a single connection.
            options: How the transaction runs - the defaults for None.

        Returns:
            The transaction.
        """
        return cls(partial(cls.get_connection, using), options if options is not None else TransactionOptions.DEFAULT)

    @staticmethod
    def _default_connection_name(connection_handler: ConnectionHandler) -> str | None:
        """The connection to use when several are configured and none is given: the one every
        registered app's models resolve to, else one named "default".
        """
        apps = HareContext.require_current().apps
        if apps is not None:
            app_default_connections = {model._meta.default_connection for model in apps.get_models_iterable()}
            if len(app_default_connections) == 1:
                return next(iter(app_default_connections))
        if DEFAULT_CONNECTION_NAME in connection_handler.db_config:
            return DEFAULT_CONNECTION_NAME
        return None

    @staticmethod
    def get_connection(using: str | None) -> DatabaseClient:
        """The connection a transaction runs on.

        Args:
            using: The connection's name; None picks the only connection, or the one every app's
                models use.

        Returns:
            The connection.

        Raises:
            QueryError: No name is given and several connections could be meant.
        """
        connection_handler = Connections.current()
        if using:
            return connection_handler.get(using)
        single_client = connection_handler.get_single_client()
        if single_client is not None:
            return single_client
        if default_connection_name := Atomic._default_connection_name(connection_handler):
            return connection_handler.get(default_connection_name)
        raise connection_handler.get_ambiguous_connection_error()

    def __call__(self, function: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        if not self._retries:

            @wraps(function)
            async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
                async with Atomic(self._get_connection, self._options):
                    return await function(*args, **kwargs)

            return wrapped

        @wraps(function)
        async def retried(*args: P.args, **kwargs: P.kwargs) -> T:
            result: Any = None
            async for attempt in AtomicAttempts(self._get_connection, self._options, self._retries):
                async with attempt:
                    result = await function(*args, **kwargs)
            return result

        return retried
