from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial, wraps
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from hare.core.connection_handler import ConnectionHandler
from hare.core.connections import Connections
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.core.context import HareContext
from hare.exceptions import QueryError, TransactionManagementError, UnSupportedError
from hare.transactions.options import TransactionOptions

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.dialects.base.transaction_context import TransactionContext

T = TypeVar("T")
P = ParamSpec("P")


class Atomic:
    """One transaction on a connection: ``async with`` runs its block in the transaction, and as a
    decorator it runs every call of the function in a transaction of its own.

    Nested inside another transaction of the same connection it is a savepoint.

    Args:
        get_connection: Gives the connection the transaction runs on, when it starts.
        options: How the transaction runs.
    """

    __slots__ = ("_get_connection", "_options", "_context")

    def __init__(self, get_connection: Callable[[], DatabaseClient], options: TransactionOptions) -> None:
        self._get_connection = get_connection
        self._options = options
        self._context: TransactionContext | None = None

    async def __aenter__(self) -> TransactionClient:
        if self._context is not None:
            raise TransactionManagementError("This transaction was already used - create a new one with atomic()")
        connection = self._get_connection()
        if not connection.features.supports_transactions:
            raise UnSupportedError(
                f"{connection.connection_name!r} is a {connection.dialect} database, which has no transactions"
            )
        if self._options.isolation is not None:
            connection.dialect.get_isolation_level(self._options.isolation)
        self._context = connection._in_transaction(self._options)
        return await self._context.__aenter__()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._context is None:
            raise TransactionManagementError("This transaction was never started")
        await self._context.__aexit__(exc_type, exc_val, exc_tb)

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
    def _default_connection_name(conn_handler: ConnectionHandler) -> str | None:
        """The connection to use when several are configured and none is given: the one every
        registered app's models resolve to, else one named "default".
        """
        apps = HareContext.require_current().apps
        if apps is not None:
            app_default_connections = {model._meta.default_connection for model in apps.get_models_iterable()}
            if len(app_default_connections) == 1:
                return next(iter(app_default_connections))
        if DEFAULT_CONNECTION_NAME in conn_handler.db_config:
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
        conn_handler = Connections.current()
        if using:
            connection = conn_handler.get(using)
        elif len(conn_handler.db_config) == 1:
            connection = conn_handler.get(next(iter(conn_handler.db_config.keys())))
        elif default_connection_name := Atomic._default_connection_name(conn_handler):
            connection = conn_handler.get(default_connection_name)
        else:
            raise QueryError(
                f"You are running with multiple databases, so you should specify using: {list(conn_handler.db_config)}"
            )
        return connection

    def __call__(self, func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(func)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            async with Atomic(self._get_connection, self._options):
                return await func(*args, **kwargs)

        return wrapped
