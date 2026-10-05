from __future__ import annotations

import abc
import asyncio
import contextlib
import contextvars
import datetime
import decimal
import time
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from functools import partial, wraps
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.log import db_client_logger
from hare.dialects.base.client.connection_ping import ConnectionPing
from hare.dialects.base.client.constants import PING_TIMEOUT_SECONDS
from hare.dialects.base.client.pool.pool_statistics import PoolStatistics
from hare.dialects.base.constants import (
    COMMAND_TIMEOUT_METHOD_NAMES,
    OBSERVED_METHOD_NAMES,
    QUERY_EXECUTING_METHOD_NAMES,
)
from hare.dialects.base.dialect import Dialect
from hare.dialects.base.features import Features
from hare.dialects.base.results.described_result import DescribedResult
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.base.transactions.contexts.non_transactional_context import NonTransactionalContext
from hare.dialects.base.transactions.contexts.top_level_transaction_context import TopLevelTransactionContext
from hare.dialects.base.transactions.contexts.transaction_context import TransactionContext
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import (
    OperationalError,
    TransactionManagementError,
    TransactionRetryError,
    UnSupportedError,
)
from hare.instrumentation.declarations import PoolStatus, QueryCall
from hare.instrumentation.enums import PoolRole
from hare.instrumentation.observers.observers import Observers
from hare.instrumentation.pools.pool_registry import PoolRegistry
from hare.instrumentation.queries.query_tags import QueryTags
from hare.sql import Query
from hare.transactions.transaction_options import TransactionOptions

if TYPE_CHECKING:
    from hare.core.connections.connection_handler import ConnectionHandler
    from hare.dialects.base.client.declarations import RowLockOutcome, ShellCommand
    from hare.dialects.base.client.password_provider import PasswordProvider
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.dialects.base.connection.driver import Driver
    from hare.models import Model
from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.client.pool.pool_connection_wrapper import PoolConnectionWrapper

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
    connection_alias: str
    query_class: type[Query] = Query
    #: The Python types the driver returns a column's value as already - a field of such a type
    #: reads it without converting.
    native_python_types: ClassVar[frozenset[type]] = frozenset(
        {bytes, str, int, float, decimal.Decimal, datetime.datetime, datetime.date}
    )
    #: Whether the client runs its statements inside a transaction - a ``TransactionClient``. Read on
    #: every statement: ``isinstance()`` against the abstract class costs two calls more.
    is_transaction_client: ClassVar[bool] = False
    #: How many times a read that lost its connection is retried on a fresh one - set by the
    #: clients that retry; none by default.
    read_retry_max_retries: int = 0
    #: The tenant schema settings - set by __init__; a transaction client takes its connection's.
    tenant_schema_template: str | None = None
    tenant_schema: str | None = None
    tenant_client_settings: Mapping[str, Any] = MappingProxyType({})
    tenant_row_level_security: bool = False
    #: Which client of its connection this one is - set by whoever makes it; a transaction client
    #: takes its connection's.
    pool_role: PoolRole = PoolRole.OWN
    #: The connection handler that made the client - the pools of a context are its handler's.
    connection_handler: ConnectionHandler | None = None
    dialect: Dialect
    features: Features = Features()
    #: Whether the client cancels an over-running statement itself, without a statement of the
    #: dialect's - a statement-time-limited transaction then needs no setup statement.
    enforces_statement_timeout_itself: ClassVar[bool] = False
    #: Whether the client applies a transaction's lock timeout itself, without a statement of
    #: the dialect's - SQLite's sets the connection's busy timeout.
    enforces_lock_timeout_itself: ClassVar[bool] = False
    driver_name: str
    #: Where the client takes its password from - None for a fixed password
    #: (``password_provider``).
    password_provider: PasswordProvider | None = None

    #: A character the dialect refuses in a statement's text before it is sent - None for none
    #: (``raise_rejected_sql_character()``).
    rejected_sql_character: ClassVar[str | None] = None
    #: Whether a statement on a transaction client checks first that the transaction isn't
    #: aborted (``_is_transaction_aborted()``).
    checks_aborted_transactions: ClassVar[bool] = False
    #: The message of the error a statement in an aborted transaction raises.
    aborted_transaction_message: ClassVar[str] = ""
    #: Whether the client's statements take the statement options - ``command_timeout``,
    #: ``password_provider``, ``transaction_pooling``, ``read_retry_max_retries``.
    runs_statement_options: ClassVar[bool] = False
    #: Runs a method once more with a new password when the server refused the pool's -
    #: ``(method, client, *args, **kwargs)``.
    run_renewing_refused_password: ClassVar[Callable[..., Any] | None] = None
    #: Runs a statement past a transaction pooler that lost its prepared plan -
    #: ``(method, client, sql, *args, **kwargs)``.
    stale_statement_runner: ClassVar[Callable[..., Any] | None] = None
    #: The longest a statement may run, cancelled by the client itself - None for no limit.
    command_timeout: float | None = None
    #: Whether the connection goes through a pooler sharing server connections by transaction.
    transaction_pooling: bool = False

    @classmethod
    def translate_exceptions(cls, function: Callable[..., Any]) -> Callable[..., Any]:
        """Wraps a client method: appends the query tags to its SQL, runs it inside the query
        wrappers, raises the driver's exceptions as hare's (``translate_driver_error()``) and
        reports the query to the observers. The dialect's hooks are read once, here - ``cls`` is the
        dialect's client class the decorator is taken from.

        Args:
            function: The method.

        Returns:
            The wrapped method.
        """
        method_name = function.__name__
        is_query_executing = method_name in QUERY_EXECUTING_METHOD_NAMES
        is_observed = method_name in OBSERVED_METHOD_NAMES
        is_command_timeout_bounded = method_name in COMMAND_TIMEOUT_METHOD_NAMES
        runs_statement_options = cls.runs_statement_options
        run_renewing_refused_password = cls.run_renewing_refused_password
        stale_statement_runner = cls.stale_statement_runner
        translate_driver_error = cls.translate_driver_error
        rejected_character = cls.rejected_sql_character
        checks_aborted_transactions = cls.checks_aborted_transactions

        def get_translating_method(wrapped_method: Callable[..., Any] | None) -> Callable[..., Any]:
            # wrapped_method: the method the query wrappers run, its SQL tagged already - None
            # for that method itself.
            tags_sql = wrapped_method is not None
            runs_query_wrappers = is_observed and wrapped_method is not None

            async def bound_by_command_timeout(client: Any, *args: Any, **kwargs: Any) -> Any:
                return await client._run_with_command_timeout(function, *args, **kwargs)

            async def translating_method(self: Any, *args: Any, **kwargs: Any) -> Any:
                if is_query_executing and not (tags_sql and QueryTags.current.get()):
                    sql = args[0] if args else kwargs.get("query")
                    parameters = args[1] if len(args) > 1 else kwargs.get("values")
                else:
                    args, kwargs, sql, parameters = DatabaseClient.get_tagged_query_arguments(
                        args, kwargs, method_name, self
                    )
                if runs_query_wrappers and Observers.query_wrappers:
                    call = QueryCall(method_name, cast("str", sql), parameters, self.connection_alias, self.dialect)
                    return await Observers.run_wrapped(
                        call, partial(cast("Callable[..., Any]", wrapped_method), self, *args, **kwargs)
                    )
                if rejected_character is not None and sql is not None and rejected_character in sql:
                    # Refused before it is sent, so an open transaction isn't aborted by it.
                    self.raise_rejected_sql_character(sql)
                start = time.perf_counter()
                exception: Exception | None = None
                # False for the read retry, which records each of its attempts itself.
                record_here = True
                is_transaction_client = self.is_transaction_client
                try:
                    # A stale reference to a finished transaction or savepoint must not run a query -
                    # its connection may already serve another transaction.
                    if is_query_executing and is_transaction_client:
                        self._check_statement_allowed()
                        if checks_aborted_transactions and self._is_transaction_aborted():
                            raise TransactionManagementError(self.aborted_transaction_message)
                    if not runs_statement_options:
                        return await function(self, *args, **kwargs)
                    method: Callable[..., Any] = (
                        bound_by_command_timeout
                        if is_command_timeout_bounded and self.command_timeout is not None
                        else function
                    )
                    if self.password_provider is not None and not is_transaction_client:
                        method = partial(cast("Callable[..., Any]", run_renewing_refused_password), method)
                    if is_query_executing and self.transaction_pooling and not is_transaction_client:
                        method = partial(cast("Callable[..., Any]", stale_statement_runner), method)
                    # Only a client outside a transaction retries a read: a transaction's pinned
                    # connection is dead, and the read would run outside its isolation.
                    if (
                        is_query_executing
                        and self.read_retry_max_retries
                        and not is_transaction_client
                        and retryable_read_query_active.get()
                    ):
                        record_here = False
                        return await self._execute_read_query_with_retry(
                            method, *args, sql=sql, parameters=parameters, **kwargs
                        )
                    return await method(self, *args, **kwargs)
                except BaseException as driver_error:
                    error = translate_driver_error(self, driver_error, sql, parameters, args, is_query_executing)
                    if isinstance(error, Exception):
                        exception = error
                    if error is driver_error:
                        raise
                    raise error from driver_error
                finally:
                    if is_observed and record_here:
                        Observers.record_query(sql, parameters, start, exception, self.connection_alias)

            return translating_method

        return wraps(function)(get_translating_method(get_translating_method(None)))

    @staticmethod
    def translate_driver_error(
        client: Any,
        error: BaseException,
        sql: str | None,
        parameters: Any,
        args: tuple[Any, ...],
        is_query_executing: bool,
    ) -> BaseException:
        """The hare exception of what a wrapped method raised - the error itself to raise it as it is.

        Args:
            client: The client the method ran on.
            error: The exception.
            sql: The statement, None for a method running none.
            parameters: Its parameters.
            args: The method's positional arguments.
            is_query_executing: Whether the method runs a statement.

        Returns:
            The exception to raise.
        """
        return error

    def raise_rejected_sql_character(self, sql: str) -> None:
        """Raises for a statement holding ``rejected_sql_character``.

        Args:
            sql: The statement.
        """
        raise NotImplementedError

    @staticmethod
    def get_tagged_query_arguments(
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        method_name: str,
        client: Any,
    ) -> tuple[tuple[Any, ...], dict[str, Any], str | None, Any]:
        """Reads the SQL and its parameters off the arguments of a client method the dialect's
        exception-translating decorator wraps, and appends the active query tags
        (``QueryTags``) to the SQL. A bulk load's SQL is the dialect's statement of it
        (``QueryClauses.get_bulk_load_statement_sql()``); a method running no query - commit(),
        rollback() - has none.

        Args:
            args: The method's positional arguments - the SQL first, its parameters second.
            kwargs: Its keyword arguments - ``query`` and ``values``.
            method_name: The method.
            client: The client the method is called on - its dialect names a bulk load.

        Returns:
            The arguments with the tagged SQL, the SQL, and its parameters - None for no query.
        """
        if method_name == "copy":
            table = args[0] if args else kwargs["table"]
            columns = args[1] if len(args) > 1 else kwargs["columns"]
            return args, kwargs, client.dialect.clauses.get_bulk_load_statement_sql(table, columns), None
        if method_name not in QUERY_EXECUTING_METHOD_NAMES:
            return args, kwargs, None, None
        sql = args[0] if args else kwargs.get("query")
        parameters = args[1] if len(args) > 1 else kwargs.get("values")
        if sql is not None:
            tagged_sql = QueryTags.append(sql)
            if tagged_sql is not sql:
                if args:
                    args = (tagged_sql, *args[1:])
                else:
                    kwargs = {**kwargs, "query": tagged_sql}
                sql = tagged_sql
        return args, kwargs, sql, parameters

    def __init__(self, connection_alias: str, fetch_inserted: bool = True, **kwargs: Any) -> None:
        self.log = db_client_logger
        self.connection_alias = connection_alias
        self.fetch_inserted = fetch_inserted
        #: The schema name template of the tenants' schemas (``tenant_{tenant}``) - None when the
        #: connection has no schema per tenant.
        self.tenant_schema_template: str | None = None
        #: The tenant schema this client works in - None for a connection's own client.
        self.tenant_schema: str | None = None
        #: The settings over its connection's own this client of a tenant schema was made with.
        self.tenant_client_settings: Mapping[str, Any] = MappingProxyType({})
        #: Whether a transaction sets its tenants for the ``TenantCondition`` policies.
        self.tenant_row_level_security = False
        #: The client of each tenant schema, made on its first use - closed with this client.
        self.tenant_clients: dict[str, DatabaseClient] = {}
        #: What the client's pool has done - kept across reconnects, which replace the pool.
        self.pool_statistics: Any = self.create_pool_statistics()

    def get_tenant_client(self) -> DatabaseClient:
        """The client the active tenant scope works through - the client of the tenant's schema when
        the connection has a schema per tenant and the scope is one tenant, else this client.

        Returns:
            The client.
        """
        if self.tenant_schema is not None or self.tenant_schema_template is None:
            return self
        # Local import: the tenant schemas import the models, which import the dialects.
        from hare.models.tenancy.tenant_schemas import TenantSchemas

        schema_name = TenantSchemas.get_active_schema_name(self)
        if schema_name is None:
            return self
        return self.get_schema_client(schema_name)

    def get_schema_client(self, schema_name: str) -> DatabaseClient:
        """The client of one tenant's schema, made on its first use.

        Args:
            schema_name: The tenant's schema.

        Returns:
            The client.

        Raises:
            UnSupportedError: The database has no schemas per tenant.
        """
        tenant_client = self.tenant_clients.get(schema_name)
        if tenant_client is None:
            tenant_client = self.tenant_clients[schema_name] = self._create_client(
                {}, self.get_tenant_client_settings(schema_name), schema_name
            )
            tenant_client.pool_role = PoolRole.TENANT_SCHEMA
        return tenant_client

    def create_independent_client(self, settings: Mapping[str, Any] | None = None) -> DatabaseClient:
        """A new connection to this client's database, apart from it and in the same tenant schema.
        The caller closes it.

        Args:
            settings: Settings replacing the connection's own.

        Returns:
            The client.
        """
        return self._create_client(settings or {}, self.tenant_client_settings, self.tenant_schema)

    def _create_client(
        self, settings: Mapping[str, Any], tenant_client_settings: Mapping[str, Any], tenant_schema: str | None
    ) -> DatabaseClient:
        """A new connection to this client's database.

        Args:
            settings: Settings replacing the connection's own.
            tenant_client_settings: The settings of the tenant schema it works in.
            tenant_schema: The tenant schema it works in, None for none.

        Returns:
            The client.
        """
        # Local import: the connections import the dialects.
        from hare.core.connections.connections import Connections

        client = Connections.current().create_independent(
            self.connection_alias, {**settings, **tenant_client_settings}
        )
        client.tenant_schema_template = self.tenant_schema_template
        client.tenant_schema = tenant_schema
        client.tenant_client_settings = tenant_client_settings
        return client

    def get_tenant_client_settings(self, schema_name: str) -> dict[str, Any]:
        """The connection settings of a tenant schema's client - its search path.

        Args:
            schema_name: The tenant's schema.

        Returns:
            The settings replacing the connection's own.

        Raises:
            UnSupportedError: The database has no schemas per tenant.
        """
        raise UnSupportedError(f"The {self.dialect} dialect has no schema per tenant")

    async def close_tenant_clients(self) -> None:
        """Closes the clients of the tenant schemas."""
        tenant_clients = list(self.tenant_clients.values())
        self.tenant_clients.clear()
        for tenant_client in tenant_clients:
            await tenant_client.close()

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
        PoolRegistry.add(self)
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

    def create_pool_statistics(self) -> Any:
        """The object counting what the client's pool does - a driver whose pool lives outside Python
        returns its own, with ``PoolStatistics``'s methods.

        Returns:
            The statistics.
        """
        return PoolStatistics()

    def get_pool_status(self) -> PoolStatus | None:
        """What the client's pool of connections holds and has done right now.

        Returns:
            The status, None while the pool isn't open.

        Raises:
            UnSupportedError: The client reports no pool status (``Features.supports_pool_status``).
        """
        if not self.features.supports_pool_status:
            raise UnSupportedError(
                f"The {self.driver_name} client of {self.connection_alias!r} reports no pool status"
            )
        occupancy = self.get_pool_occupancy()
        if occupancy is None:
            return None
        size, idle, waiting, min_size, max_size = occupancy
        acquire_count, acquire_timeouts, acquire_wait_seconds_total, connect_count, connect_failures = (
            self.pool_statistics.get_counts()
        )
        return PoolStatus(
            self.connection_alias,
            self.pool_role,
            self.tenant_schema,
            size,
            idle,
            size - idle,
            waiting,
            min_size,
            max_size,
            acquire_count,
            acquire_timeouts,
            acquire_wait_seconds_total,
            connect_count,
            connect_failures,
        )

    def get_server_client(self) -> DatabaseClient:
        """The client of the database server itself, past any pooler the connection goes through -
        this client for a connection going straight to the server.

        Returns:
            The client.
        """
        return self

    def apply_password(self, password: str) -> None:
        """Opens the client's new connections with ``password`` from now on - a renewed password of
        ``password_provider``. Nothing for a driver that asks the provider itself for every
        connection it opens.

        Args:
            password: The password.
        """

    async def get_shell_command(self) -> ShellCommand | None:
        """The interactive client of the database this connection opens (``hare dbshell``) - None
        for a dialect that names none.

        Returns:
            The command.

        Raises:
            UnSupportedError: The connection's database can't be opened by another program.
        """
        return None

    async def renew_password(self) -> None:
        """Asks ``password_provider`` for a new password after the server refused the last one, and
        opens the new connections with it (``apply_password()``)."""
        provider = self.password_provider
        if provider is not None:
            self.apply_password(await provider.fetch(provider.password))

    def get_address(self) -> str:
        """The server's address for reports (``ConnectionFailed``) - ``host:port``, a file; empty for
        a client that names none.

        Returns:
            The address.
        """
        return ""

    def get_pool_occupancy(self) -> tuple[int, int, int, int, int] | None:
        """How the client's pool is taken - for ``get_pool_status()`` of a client with
        ``Features.supports_pool_status``.

        Returns:
            The open, the idle and the waited-for connections, the pool's least and most size; None
            while the pool isn't open.
        """
        raise NotImplementedError()  # pragma: nocoverage

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

    def _get_transaction_client(self) -> TransactionClient:
        """The driver's client of a top-level transaction opened on this client."""
        raise NotImplementedError()  # pragma: nocoverage

    def _get_transaction_restriction_statements(self, options: TransactionOptions) -> list[str]:
        """Returns the statements that make a freshly begun transaction run at its isolation level
        - the dialect's own level for it - read-only and time-limited, from the dialect's
        ``TransactionStatements``. The isolation statement comes first: it must precede every query
        of the transaction. A statement-time-limited transaction is also lock-time-limited, so a
        statement waiting on a lock is cancelled as well.

        Args:
            options: The transaction's options.

        Returns:
            The statements to run right after BEGIN, in order.

        Raises:
            UnSupportedError: A restriction is asked for that the dialect has no statement for and
                this client doesn't enforce itself.
        """
        transactions = self.dialect.transactions
        statements: list[str] = []
        if options.isolation is not None:
            isolation_sql = transactions.get_isolation_level_sql(transactions.get_isolation_level(options.isolation))
            if isolation_sql is not None:
                statements.append(isolation_sql)
        if options.read_only:
            read_only_sql = transactions.get_read_only_sql()
            if read_only_sql is None:
                raise UnSupportedError(f"{self.dialect.name} has no support for read-only transactions")
            statements.append(read_only_sql)
        if (statement_timeout := options.statement_timeout) is not None:
            milliseconds = max(1, round(statement_timeout * 1000))
            statement_timeout_sql = transactions.get_statement_timeout_sql(milliseconds)
            if statement_timeout_sql is not None:
                statements.append(statement_timeout_sql)
                lock_timeout_sql = transactions.get_lock_timeout_sql(milliseconds)
                if lock_timeout_sql is not None and options.lock_timeout is None:
                    statements.append(lock_timeout_sql)
            elif not self.enforces_statement_timeout_itself:
                raise UnSupportedError(f"{self.dialect.name} has no support for statement-timeout transactions")
        if (lock_timeout := options.lock_timeout) is not None:
            lock_timeout_sql = transactions.get_lock_timeout_sql(max(1, round(lock_timeout * 1000)))
            if lock_timeout_sql is not None:
                statements.append(lock_timeout_sql)
            elif not self.enforces_lock_timeout_itself:
                raise UnSupportedError(f"{self.dialect.name} has no support for lock-timeout transactions")
        return statements

    def lock_timeout_session(self, seconds: float) -> AbstractAsyncContextManager[DatabaseClient]:
        """A client on one connection whose every statement, in a transaction or not, waits at most
        ``seconds`` for a lock another session holds - for a migration running outside a
        transaction.

        Args:
            seconds: The lock timeout.

        Returns:
            The context giving the client.

        Raises:
            UnSupportedError: The database has no lock timeout outside a transaction - by default.
        """
        raise UnSupportedError(f"{self.dialect.name} has no lock timeout outside a transaction")

    #: Turns one row of ``execute()`` into a dict - a driver whose rows have a faster way sets it.
    row_to_dict: Callable[[Any], dict[str, Any]] = dict

    async def execute(
        self,
        query: str,
        values: list[Any] | None = None,
        *,
        returns_rows: bool | None = None,
        rows_by_position: bool = False,
    ) -> StatementResult:
        """Executes one SQL statement.

        Args:
            query: The SQL string, pre-parametrized for the target DB dialect.
            values: A sequence of positional DB parameters.
            returns_rows: Whether the statement returns rows - True for a SELECT or a write with
                ``RETURNING``, False for any other write. None lets the driver find out from the
                SQL text; a caller that knows spares that.
            rows_by_position: The caller reads the rows by position alone, their column names from
                ``StatementResult.column_names`` - a driver whose rows read by name cost more may
                then give plain tuples (``Features.supports_positional_rows``).

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

    async def stream_batches(
        self, query: str, values: list[Any] | None = None, chunk_size: int = 0
    ) -> AsyncGenerator[list[Any]]:
        """Runs ``query`` on a server-side cursor, yielding the raw rows a batch at a time as they
        arrive. Appends the query tags, runs the query wrappers around the whole iteration and
        reports one ``QueryExecuted`` once the stream is read to the end or closed.

        Args:
            query: The SQL.
            values: The bound values.
            chunk_size: How many rows a batch holds - the driver's own default when 0.
        """
        query = QueryTags.append(query)
        start = time.perf_counter()
        error: Exception | None = None
        if Observers.query_wrappers:
            call = QueryCall("stream", query, values, self.connection_alias, self.dialect)
            batches = Observers.stream_wrapped(call, lambda: self._driver_stream_batches(query, values, chunk_size))
        else:
            batches = self._driver_stream_batches(query, values, chunk_size)
        try:
            async for batch in batches:
                yield batch
        except Exception as raised_error:
            error = raised_error
            raise
        finally:
            try:
                if (close_batches := getattr(batches, "aclose", None)) is not None:
                    await close_batches()
            finally:
                Observers.record_query(query, values, start, error, self.connection_alias)

    async def stream(self, query: str, values: list[Any] | None = None, chunk_size: int = 0) -> AsyncGenerator[Any]:
        """``stream_batches()`` a row at a time - reading a row after the stream's transaction ended
        raises.

        Args:
            query: The SQL.
            values: The bound values.
            chunk_size: How many rows to fetch per round trip - the driver's own default when 0.
        """
        async with contextlib.aclosing(self.stream_batches(query, values, chunk_size)) as batches:
            async for batch in batches:
                for row in batch:
                    self.check_stream_open()
                    yield row

    def check_stream_open(self) -> None:
        """Refuses to hand out a streamed row the connection no longer reads - nothing by default: a
        stream outside a transaction is read to its end or closed.

        Raises:
            TransactionManagementError: The transaction the stream belongs to ended.
        """

    async def _driver_stream_batches(
        self, query: str, values: list[Any] | None = None, chunk_size: int = 0
    ) -> AsyncGenerator[list[Any]]:
        """The driver's server-side cursor behind ``stream_batches()`` - implemented by a driver that
        supports streaming.

        Args:
            query: The tagged SQL.
            values: The bound values.
            chunk_size: How many rows a batch holds - the driver's own default when 0.
        """
        raise UnSupportedError(f"{self.dialect.name} has no server-side streaming support")
        yield  # type: ignore[unreachable]  # pragma: nocoverage - makes this an async generator.

    async def take_generated_keys(self, model: type[Model], count: int) -> list[Any]:
        """Takes the next keys of a model's series - of a database handing them out before the rows
        are written (``Features.takes_keys_before_insert``).

        Args:
            model: The model.
            count: How many keys.

        Returns:
            The keys, each never handed out again.

        Raises:
            UnSupportedError: The database generates keys as it writes the rows.
        """
        raise UnSupportedError(f"The {self.dialect} database generates keys as it writes the rows")

    async def take_row_locks(self, lock_names: Sequence[str], *, wait: bool) -> RowLockOutcome:
        """Takes, for the transaction, the locks of rows by their names - of a database locking rows by
        their keys outside SQL (``Features.locks_rows_by_key``). The locks are given back as the
        transaction ends.

        Args:
            lock_names: The names - the table and the key of a row each.
            wait: Whether to wait for a lock another transaction holds - else it is reported busy.

        Returns:
            What was taken.

        Raises:
            UnSupportedError: The database locks rows in SQL.
        """
        raise UnSupportedError(f"The {self.dialect} database locks rows in SQL")

    async def synchronize_key_series(self, model: type[Model]) -> None:
        """Moves a model's series of keys past the greatest key its table holds - rows written with
        keys of their own leave the series behind. Nothing by default: a database generating keys as
        it writes the rows keeps up by itself.

        Args:
            model: The model.
        """

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
        Bulk-loads rows into a table through a driver-native bulk-load protocol, bypassing
        parameterized INSERT entirely - no ON CONFLICT/RETURNING support.

        Args:
            table: The bare (unquoted) table name to load into.
            columns: The DB column names to load, in the same order as each record's values.
            records: One tuple of already DB-ready values per row.
            column_types: Each column's type as the dialect names it for a bulk load
                (``SqlParameters.get_copy_column_type()``), same order as ``columns`` - needed by
                a driver whose bulk-load protocol has no per-value type negotiation of its own.
        """
        raise NotImplementedError()  # pragma: nocoverage

    def get_schema_sql(self, safe: bool) -> str:
        """The DDL creating every model of this connection.

        Args:
            safe: Whether each object is created only when it doesn't exist yet.

        Returns:
            The DDL script.
        """
        return self.dialect.schema_editor_class(self).table_creation.get_create_schema_sql(safe)

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
        return (await ConnectionPing.run(self, timeout)).succeeded
