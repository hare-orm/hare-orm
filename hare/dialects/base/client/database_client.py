import abc
import asyncio
import contextvars
import datetime
import decimal
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.log import db_client_logger
from hare.dialects.base.constants import (
    PING_TIMEOUT_SECONDS,
    QUERY_EXECUTING_METHOD_NAMES,
)
from hare.dialects.base.dialect import Dialect
from hare.dialects.base.features import Features
from hare.dialects.base.non_transactional_context import NonTransactionalContext
from hare.dialects.base.results.described_result import DescribedResult
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.base.top_level_transaction_context import TopLevelTransactionContext
from hare.dialects.base.transaction_context import TransactionContext
from hare.dialects.registry import DialectRegistry
from hare.exceptions import (
    DatabaseError,
    OperationalError,
    TransactionRetryError,
    UnSupportedError,
)
from hare.instrumentation.constants import COPY_STATEMENT_TEMPLATE
from hare.instrumentation.query_tags import QueryTags
from hare.sql import Query
from hare.transactions.options import TransactionOptions

if TYPE_CHECKING:
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.dialects.base.driver import Driver
from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.client.pool_connection_wrapper import PoolConnectionWrapper

#: Set while a read-only query runs - a connection lost during it may be retried on a fresh
#: connection. A write never sets it.
retryable_read_query_active: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "retryable_read_query_active", default=False
)


class DatabaseClient(abc.ABC):
    """
    Base class for containing a DB connection.

    Parameters get passed as kwargs, and is mostly driver specific.

    Attributes:
        query_class: The Query dialect (low level dialect).
        dialect: The SQL dialect of the database.
        features: What the database and the driver support.
        driver_name: The name of the driver the client belongs to (``Driver.name``).
    """

    _connection: Any
    _parent: DatabaseClient
    _pool: Any
    _bound_loop: asyncio.AbstractEventLoop | None = None
    connection_name: str
    query_class: type[Query] = Query
    #: The Python types the driver returns a column's value as already - a field of such a type
    #: reads it without converting.
    native_python_types: ClassVar[frozenset[type]] = frozenset(
        {bytes, str, int, float, decimal.Decimal, datetime.datetime, datetime.date}
    )
    #: Whether ``copy()`` takes part in a surrounding transaction, so a
    #: ``bulk_create(use_copy=True)`` split into several COPY statements stays all-or-nothing.
    copy_joins_transaction: ClassVar[bool] = True
    #: Whether the client runs its statements inside a transaction - a ``TransactionClient``. Read on
    #: every statement: ``isinstance()`` against the abstract class costs two calls more.
    is_transaction_client: ClassVar[bool] = False
    #: How many times a read that lost its connection is retried on a fresh one - set by the
    #: clients that retry; none by default.
    read_retry_max_retries: int = 0
    dialect: Dialect
    features: Features = Features()
    driver_name: str

    @staticmethod
    def get_tagged_query_arguments(
        args: tuple[Any, ...], kwargs: dict[str, Any], method_name: str
    ) -> tuple[tuple[Any, ...], dict[str, Any], str | None, Any]:
        """Reads the SQL and its parameters off the arguments of a client method the dialect's
        exception-translating decorator wraps, and appends the active query tags
        (``QueryTags``) to the SQL. A bulk COPY load's SQL is ``COPY <table> (<columns>) FROM
        STDIN``; a method running no query - commit(), rollback() - has none.

        Args:
            args: The method's positional arguments - the SQL first, its parameters second.
            kwargs: Its keyword arguments - ``query`` and ``values``.
            method_name: The method.

        Returns:
            The arguments with the tagged SQL, the SQL, and its parameters - None for no query.
        """
        if method_name == "copy":
            table = args[0] if args else kwargs["table"]
            columns = args[1] if len(args) > 1 else kwargs["columns"]
            return args, kwargs, COPY_STATEMENT_TEMPLATE.format(table=table, columns=", ".join(columns)), None
        if method_name not in QUERY_EXECUTING_METHOD_NAMES:
            return args, kwargs, None, None
        sql = args[0] if args else kwargs.get("query")
        params = args[1] if len(args) > 1 else kwargs.get("values")
        if sql is not None:
            tagged_sql = QueryTags.append(sql)
            if tagged_sql is not sql:
                if args:
                    args = (tagged_sql, *args[1:])
                else:
                    kwargs = {**kwargs, "query": tagged_sql}
                sql = tagged_sql
        return args, kwargs, sql, params

    def __init__(self, connection_name: str, fetch_inserted: bool = True, **kwargs: Any) -> None:
        self.log = db_client_logger
        self.connection_name = connection_name
        self.fetch_inserted = fetch_inserted

    def _check_loop(self) -> bool:
        """Check if the current event loop matches the one this client was created on."""
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            return True  # No running loop — can't validate
        if self._bound_loop is None:
            return True  # Not yet bound (pool not created yet)
        return self._bound_loop is current

    async def _post_connect(self) -> None:
        """Called after pool/connection is created. Records the bound loop, refuses a server older
        than the dialect runs on (``Dialect.check_server_version()``, closing what was just
        opened) and applies the features the server's version changes
        (``Dialect.get_server_version_features()``).

        Raises:
            UnSupportedError: The server is older than the dialect's ``minimum_server_version``.
        """
        self._bound_loop = asyncio.get_running_loop()
        server_version = await self.get_server_version()
        if server_version is None:
            return
        try:
            self.dialect.check_server_version(server_version)
        except UnSupportedError:
            await self.close()
            raise
        changed_features = {
            name: value
            for name, value in self.dialect.get_server_version_features(server_version).items()
            if getattr(self.features, name) != value
        }
        if changed_features:
            self.features = self.features.replace(**changed_features)

    async def get_server_version(self) -> tuple[int, ...] | None:
        """Returns the version of the server the just-opened connection talks to, without going
        through query instrumentation.

        Returns:
            The version as ``(major, minor, ...)``, None when the driver doesn't know it - the
            version is then neither checked nor used to decide features.
        """
        return None

    async def create_connection(self, with_db: bool) -> None:
        """
        Establish a DB connection.

        Args:
            with_db: If True, then select the DB to use, else use default.
                Use case for this is to create/drop a database.
        """
        raise NotImplementedError()  # pragma: nocoverage

    async def _pool_acquire(self) -> Any:
        """
        Checks out one connection from the pool (PoolConnectionWrapper, a transaction's
        ``_take_transaction_resources()``) - only meaningful for a pooled backend
        (PostgresqlClient and its subclasses); sqlite has no pool and never calls this.
        """
        raise NotImplementedError()  # pragma: nocoverage

    async def _pool_release(self, pool: Any, connection: Any) -> None:
        """Hands a connection checked out by ``_pool_acquire()`` back to the pool it came from -
        which may no longer be ``self._pool`` once the client was closed meanwhile.

        Args:
            pool: The pool the connection was acquired from.
            connection: The connection to hand back.
        """
        await pool.release(connection)

    async def close(self) -> None:
        """
        Closes the DB connection.
        """
        raise NotImplementedError()  # pragma: nocoverage

    async def db_create(self) -> None:
        """Creates the database on the server - for the test runner. Needs
        ``create_connection(with_db=False)`` first.
        """
        raise NotImplementedError()  # pragma: nocoverage

    async def db_delete(self) -> None:
        """Drops the database on the server - for the test runner. Needs
        ``create_connection(with_db=False)`` first.
        """
        raise NotImplementedError()  # pragma: nocoverage

    def acquire_connection(self) -> ConnectionWrapper[Any] | PoolConnectionWrapper[Any]:
        """
        Acquires a connection from the pool.
        Will return the current context connection if already in a transaction.
        """
        raise NotImplementedError()  # pragma: nocoverage

    @classmethod
    def get_driver(cls) -> Driver:
        """Returns the driver the client class belongs to."""
        return DialectRegistry.get_driver(cls.driver_name)

    @classmethod
    def _get_operational_error(cls, error: BaseException, **context: Any) -> OperationalError:
        """Returns the hare exception for a statement the database refused to run.

        Args:
            error: The driver's exception.
            **context: The failed statement's ``sql``/``params``, where known.

        Returns:
            TransactionRetryError where the driver says running the transaction again can
            succeed (``Driver.is_retryable``), OperationalError otherwise.
        """
        error_class = TransactionRetryError if cls.get_driver().is_retryable(error) else OperationalError
        return error_class(error, **context)

    def _in_transaction(self, options: TransactionOptions = TransactionOptions.DEFAULT) -> TransactionContext:
        """Opens a transaction context on this client - the one hare's own multi-statement writes
        (a cascade, a many-to-many ``add()``, a ``bulk_create()`` in batches) run in.

        On a database without transactions (``Features.supports_transactions`` False) the block's
        statements run one by one, each applied at once (``NonTransactionalContext``).

        Args:
            options: How the transaction runs - read-only, statement timeout, isolation level.

        Returns:
            The context.
        """
        if not self.features.supports_transactions:
            return NonTransactionalContext(self)
        return self._begin_transaction(options)

    def _begin_transaction(self, options: TransactionOptions) -> TransactionContext:
        """Opens a transaction context on a database that has transactions.

        Args:
            options: How the transaction runs - read-only, statement timeout, isolation level.

        Returns:
            The context.
        """
        client = self._get_transaction_client()
        client._set_transaction_options(options)
        return TopLevelTransactionContext(client)

    def _get_transaction_client(self) -> "TransactionClient":
        """The driver's client of a top-level transaction opened on this client."""
        raise NotImplementedError()  # pragma: nocoverage

    def _get_isolation_statements(self, options: TransactionOptions) -> list[str]:
        """Returns the statements that make a freshly begun transaction run at the isolation
        level ``options`` asks for - the dialect's own level for it (``Dialect.get_isolation_level``).

        Args:
            options: The transaction's options.

        Returns:
            The statements to run right after BEGIN, before any other.

        Raises:
            UnSupportedError: The dialect has no isolation level at least as strong as the one asked for.
        """
        if options.isolation is None:
            return []
        level = self.dialect.get_isolation_level(options.isolation)
        isolation_sql = self.dialect.get_isolation_level_sql(level)
        return [] if isolation_sql is None else [isolation_sql]

    def _get_transaction_restriction_statements(self, options: TransactionOptions) -> list[str]:
        """Returns the statements that make a freshly begun transaction run at its isolation level,
        read-only and/or time-limited.

        Args:
            options: The transaction's options.

        Returns:
            The statements to run right after BEGIN, in order.

        Raises:
            UnSupportedError: A restriction is asked for that this client has no support for.
        """
        if options.read_only or options.statement_timeout is not None:
            raise UnSupportedError(
                f"{self.dialect.name} has no support for read-only or statement-timeout transactions"
            )
        return self._get_isolation_statements(options)

    #: Turns one row of ``execute()`` into a dict - a driver whose rows have a faster way sets it.
    row_to_dict: Callable[[Any], dict[str, Any]] = dict

    async def execute(
        self, query: str, values: list[Any] | None = None, *, returns_rows: bool | None = None
    ) -> StatementResult:
        """Executes one SQL statement.

        Args:
            query: The SQL string, pre-parametrized for the target DB dialect.
            values: A sequence of positional DB parameters.
            returns_rows: Whether the statement returns rows - True for a SELECT or a write with
                ``RETURNING``, False for any other write. None lets the driver find out from the
                SQL text; a caller that knows spares that.

        Returns:
            The affected or returned row count, and the rows.
        """
        raise NotImplementedError()  # pragma: nocoverage

    async def execute_described(self, query: str, values: list[Any] | None = None) -> DescribedResult:
        """Executes a raw SQL statement and returns what it returned with the names of its
        columns - known for an empty result too - and the rows it changed: for showing a query's
        result as a table (an SQL console). The rows are tuples in the order of the columns.

        Args:
            query: The SQL string, pre-parametrized for the target DB dialect.
            values: A sequence of positional DB parameters.

        Returns:
            The columns, the rows and the row count.
        """
        raise NotImplementedError()  # pragma: nocoverage

    async def execute_dicts(self, query: str, values: list[Any] | None = None) -> list[dict[str, Any]]:
        """Executes a statement that returns rows, and returns them as dicts.

        Args:
            query: The SQL string, pre-parametrized for the target DB dialect.
            values: A sequence of positional DB parameters.

        Returns:
            The rows.
        """
        return list(map(self.row_to_dict, (await self.execute(query, values, returns_rows=True)).rows))

    async def execute_script(self, query: str) -> None:
        """
        Executes a RAW SQL script with multiple statements, and returns nothing.

        Args:
            query: The SQL string, which will be passed on verbatim.
                Semicolons is supported here.
        """
        raise NotImplementedError()  # pragma: nocoverage

    async def execute_many(self, query: str, values: list[list[Any]]) -> None:
        """
        Executes one statement once per parameter row, and returns no data.

        Args:
            query: The SQL string, pre-parametrized for the target DB dialect.
            values: A sequence of positional DB parameters.
        """
        raise NotImplementedError()  # pragma: nocoverage

    async def copy(
        self, table: str, columns: list[str], records: list[tuple[Any, ...]], column_types: list[str]
    ) -> None:
        """
        Bulk-loads rows into a table through a driver-native bulk-load protocol (e.g. Postgres
        COPY), bypassing parameterized INSERT entirely - no ON CONFLICT/RETURNING support.

        Args:
            table: The bare (unquoted) table name to load into.
            columns: The DB column names to load, in the same order as each record's values.
            records: One tuple of already DB-ready values per row.
            column_types: Each column's base Postgres SQL type name (e.g. "INT", "UUID",
                stripped of any length/precision suffix), same order as ``columns`` - needed by a
                driver whose bulk-load protocol has no per-value type negotiation of its own.
        """
        raise NotImplementedError()  # pragma: nocoverage

    def get_schema_sql(self, safe: bool) -> str:
        """The DDL creating every model of this connection.

        Args:
            safe: Whether each object is created only when it doesn't exist yet.

        Returns:
            The DDL script.
        """
        return self.dialect.schema_editor_class(self).get_create_schema_sql(safe)

    async def generate_schema(self, safe: bool) -> None:
        """Creates every model of this connection in the database.

        Args:
            safe: Whether each object is created only when it doesn't exist yet.
        """
        # Connected first, so the DDL is written for the features of the server's own version.
        async with self.acquire_connection():
            pass
        schema = self.get_schema_sql(safe)
        self.log.debug("Creating schema: %s", schema)
        if schema:
            await self.execute_script(schema)

    async def ping(self, timeout: float = PING_TIMEOUT_SECONDS) -> bool:
        """Runs ``SELECT 1`` and reports whether the connection is usable. Only
        ``DBConnectionError``/``OperationalError``/``TimeoutError`` mean unhealthy - a programming
        error still propagates. The timeout is enforced here: a server that stopped answering
        without closing the socket never makes a driver raise.

        Args:
            timeout: Seconds to wait before reporting unhealthy.

        Returns:
            True if the query succeeded.
        """
        try:
            await asyncio.wait_for(self.execute("SELECT 1"), timeout)
        except DatabaseError, TimeoutError:
            return False
        return True
