from __future__ import annotations

import abc
import asyncio
import contextlib
import datetime
import decimal
import logging
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.log import db_client_logger
from hare.core.resource_completion import ResourceCompletion
from hare.dialects.base.client.connection_ping import ConnectionPing
from hare.dialects.base.client.constants import (
    DISCARDED_CLIENT_MESSAGE,
    PING_TIMEOUT_SECONDS,
    UNENCODABLE_SURROGATE_REASON,
)
from hare.dialects.base.client.enums import StatementForm
from hare.dialects.base.client.pool.pool_statistics import PoolStatistics
from hare.dialects.base.client.schema_cache_invalidation import SchemaCacheInvalidation
from hare.dialects.base.client.statement_execution import StatementExecution
from hare.dialects.base.dialect import Dialect
from hare.dialects.base.features import Features
from hare.dialects.base.results.described_result import DescribedResult
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.base.transactions.contexts.non_transactional_context import NonTransactionalContext
from hare.dialects.base.transactions.contexts.top_level_transaction_context import TopLevelTransactionContext
from hare.dialects.base.transactions.contexts.transaction_context import TransactionContext
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import (
    DBConnectionError,
    OperationalError,
    TransactionRetryError,
    UnSupportedError,
)
from hare.instrumentation.declarations import PoolStatus
from hare.instrumentation.enums import PoolRole
from hare.instrumentation.pools.pool_registry import PoolRegistry
from hare.sql import Query
from hare.transactions.transaction_options import TransactionOptions

if TYPE_CHECKING:
    from hare.core.config.connection_config import ConnectionConfig
    from hare.core.connections.connection_handler import ConnectionHandler
    from hare.dialects.base.client.declarations import ShellCommand
    from hare.dialects.base.client.password_provider import PasswordProvider
    from hare.dialects.base.client.statement_recovery import StatementRecovery
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.dialects.base.connection.driver import Driver
    from hare.models import Model
    from hare.sql.builder.tables.table import Table
    from hare.sql.sql_context import SqlContext
from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.client.pool.pool_connection_wrapper import PoolConnectionWrapper


class DatabaseClient(abc.ABC):
    """
    The base of the clients of a database connection: its pool, its statements and its transactions.

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
    #: Set once the connection is closed and dropped (``close_all(discard=True)``) - it opens no pool
    #: again for a task still holding it.
    discarded: bool = False

    #: The client of the same connection in the context a repeated ``Hare.init()`` set up - a query that
    #: took this client before it was discarded runs there.
    replaced_by: DatabaseClient | None = None
    #: The closes begun - a connection or pool that opened across one belongs to no client and is
    #: closed.
    closes_started: int = 0
    #: The task opening the connection or pool, while it does - its own setup statements run on it.
    opening_task: asyncio.Task[Any] | None = None
    _bound_loop: asyncio.AbstractEventLoop | None = None
    #: The server's version read when the pool opened (``get_server_version()``) - None before that,
    #: or when the driver doesn't know it.
    server_version: tuple[int, ...] | None = None
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
    #: Whether the driver's connections belong to the event loop they were opened on - a statement of a
    #: transaction from another loop is refused.
    connections_bound_to_event_loop: ClassVar[bool] = False
    #: How many times a read that lost its connection is retried on a fresh one - set by the
    #: clients that retry; none by default.
    read_retry_max_retries: int = 0
    read_retry_backoff_base_seconds: float = 0.0
    #: The tenant schema settings - set by __init__; a transaction client takes its connection's.
    tenant_schema_template: str | None = None
    tenant_schema: str | None = None
    tenant_client_settings: Mapping[str, Any] = MappingProxyType({})
    tenant_row_level_security: bool = False
    #: Which client of its connection this one is - set by whoever makes it; a transaction client
    #: takes its connection's.
    pool_role: PoolRole = PoolRole.MAIN
    #: The connection handler that made the client - the pools of a context are its handler's.
    connection_handler: ConnectionHandler | None = None
    #: Original construction settings, before driver normalization or runtime authentication.
    connection_config: ConnectionConfig | None = None
    dialect: Dialect
    features: Features = Features()
    #: Whether the client cancels an over-running statement itself, without a statement of the
    #: dialect's - a statement-time-limited transaction then needs no setup statement.
    enforces_statement_timeout_itself: ClassVar[bool] = False
    #: Whether the client applies a transaction's lock timeout itself, without a statement of
    #: the dialect's - a busy timeout of the connection, say.
    enforces_lock_timeout_itself: ClassVar[bool] = False
    driver_name: str
    #: Where the client takes its password from - None for a fixed password
    #: (``password_provider``).
    password_provider: PasswordProvider | None = None

    #: A character the dialect refuses in a statement's text before it is sent - None for none
    #: (``raise_rejected_sql_character()``).
    rejected_sql_character: ClassVar[str | None] = None
    #: The loggers of the client's driver library (``quiet_driver_loggers()``).
    driver_logger_names: ClassVar[tuple[str, ...]] = ()
    #: Whether a client made now keeps its driver library's warnings out of the output when nothing
    #: configured logging - the command line sets it: hare reports a failure itself.
    quiets_driver_loggers: ClassVar[bool] = False
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
    #: The longest a statement may run, cancelled by the client itself - None for no limit.
    command_timeout: float | None = None
    #: Whether the connection goes through a pooler sharing server connections by transaction.
    transaction_pooling: bool = False

    @property
    def parameter_binding_key(self) -> object | None:
        """Client-local conversion configuration included in statement plan keys."""
        return None

    @property
    def sql_context(self) -> SqlContext:
        """Render statements with the capabilities and placement of this connection."""
        return self.query_class.SQL_CONTEXT.copy(features=self.features)

    @property
    def sql_context_key(self) -> object:
        """Additional connection settings that change compiled SQL, beyond feature flags."""
        return ()

    @classmethod
    def translate_exceptions(cls, form: StatementForm) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Apply the statement lifecycle for an explicitly declared driver call shape.

        SQL calls carry query/values, bulk loads carry table/columns, and control calls
        carry no SQL. Names only label observer events; they do not select behavior.
        """
        return partial(StatementExecution.decorate, cls, form)

    def get_result_read_error(self, error: Exception) -> Exception | None:
        """The hare exception of an error a driver raises reading a value of a result after the
        statement returned - a driver decoding its rows when they are read.

        Args:
            error: The exception.

        Returns:
            None by default - for a driver decoding every value before the statement returns.
        """
        return None

    @staticmethod
    def is_missing_conflict_target_error(error: BaseException) -> bool:
        """Whether an INSERT failed because its ``ON CONFLICT`` names columns no unique index or
        constraint of the table has.

        Args:
            error: The exception, hare's or the driver's.

        Returns:
            False by default - for a database checking the target itself before the statement.
        """
        return False

    @staticmethod
    def is_unencodable_surrogate_error(error: BaseException) -> bool:
        """Whether a driver failed to encode a text holding a lone surrogate - raised as Python's own
        error, or as the driver's with that one as its cause.

        Args:
            error: The exception.

        Returns:
            True for such an error.
        """
        encoding_error = error if isinstance(error, UnicodeEncodeError) else error.__cause__
        return isinstance(encoding_error, UnicodeEncodeError) and encoding_error.reason == UNENCODABLE_SURROGATE_REASON

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

    def __init__(self, connection_alias: str, **kwargs: Any) -> None:
        from hare.core.config.connection_config import ConnectionConfig

        self.log = db_client_logger
        self.connection_alias = connection_alias
        self.connection_config = ConnectionConfig(engine=getattr(self, "driver_name", None), credentials=dict(kwargs))
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
        from hare.instrumentation.pools.pool_metrics_recorder import PoolMetricsRecorder

        PoolMetricsRecorder.attach(self)
        if DatabaseClient.quiets_driver_loggers:
            self.quiet_driver_loggers()

    @classmethod
    def quiet_driver_loggers(cls) -> None:
        """Gives the driver library's loggers a ``NullHandler`` when nothing configured logging -
        Python would print their warnings, a failed connect's traceback among them, through its
        last-resort handler. A configured logging still receives them."""
        if not cls.driver_logger_names or logging.getLogger().handlers:
            return
        for logger_name in cls.driver_logger_names:
            driver_logger = logging.getLogger(logger_name)
            if not driver_logger.handlers:
                driver_logger.addHandler(logging.NullHandler())

    def enforces_foreign_keys(self) -> bool:
        """Whether the database enforces foreign keys on this connection now - a hard delete cascades
        in Python where it doesn't.

        Returns:
            ``Features.supports_foreign_keys`` by default.
        """
        return self.features.supports_foreign_keys

    def hold_migration_lock(self) -> AbstractAsyncContextManager[None]:
        """Holds hare's migration lock for a ``migrate`` run on a database without a lock statement
        (``get_migration_lock_sql()``): two processes migrating one database apply their migrations
        one after the other.

        Returns:
            The hold - one holding nothing by default.
        """
        return contextlib.nullcontext()

    def get_successor(self) -> DatabaseClient | None:
        """The client a query that took this one runs on once it is discarded - the one replacing it,
        of its own class.

        Returns:
            The client, None while this one runs its queries or when nothing replaced it.
        """
        successor = self.replaced_by
        if not self.discarded or successor is None or type(successor) is not type(self):
            return None
        return successor

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
            DBConnectionError: This client was closed and dropped, and nothing replaced it - a client made
                now would open connections nothing closes.
        """
        tenant_client = self.tenant_clients.get(schema_name)
        if tenant_client is None:
            if self.discarded:
                successor = self.get_successor()
                if successor is None:
                    raise DBConnectionError(DISCARDED_CLIENT_MESSAGE.format(alias=self.connection_alias))
                return successor.get_schema_client(schema_name)
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

        connections = self.connection_handler if self.connection_handler is not None else Connections.current()
        client = connections.create_independent(
            self.connection_alias, {**settings, **tenant_client_settings}, config=self.connection_config
        )
        client.tenant_schema_template = self.tenant_schema_template
        client.tenant_schema = tenant_schema
        client.tenant_client_settings = tenant_client_settings
        return client

    async def enter_tenant_schema(self, schema_name: str) -> str:
        """Runs the rest of the transaction in another tenant's schema, in place of its own.

        Args:
            schema_name: The tenant's schema.

        Returns:
            What ``leave_tenant_schema()`` takes to come back.

        Raises:
            UnSupportedError: The database has no schemas per tenant.
        """
        raise UnSupportedError(f"The {self.dialect} dialect has no schema per tenant")

    async def leave_tenant_schema(self, previous_schemas: str) -> None:
        """Brings the transaction back to the schema ``enter_tenant_schema()`` left.

        Args:
            previous_schemas: What ``enter_tenant_schema()`` returned.

        Raises:
            UnSupportedError: The database has no schemas per tenant.
        """
        raise UnSupportedError(f"The {self.dialect} dialect has no schema per tenant")

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
        """Close every tenant client, including after a sibling failure or caller cancellation."""
        tenant_clients = list(self.tenant_clients.values())
        self.tenant_clients.clear()
        await ResourceCompletion.wait_all(*(tenant_client.close() for tenant_client in tenant_clients))

    def abandon_connections(self) -> None:
        """Drops the connections opened on an event loop that is closed - they can't be closed through
        it; nothing by default, for a driver whose connections need no event loop to close.
        """

    def holds_database_in_connection(self) -> bool:
        """Whether the database lives in the client's connection only - another connection to its
        address opens another, empty database. False by default.

        Returns:
            True for such a database.
        """
        return False

    def _check_loop(self) -> bool:
        """Whether the running event loop is the one the client was created on."""
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
        server_version = self.server_version = await self.get_server_version()
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
            The status - of no connection while the pool isn't open but failed to open; None while
            it isn't open and opened none.

        Raises:
            UnSupportedError: The client reports no pool status (``Features.supports_pool_status``).
        """
        if not self.features.supports_pool_status:
            raise UnSupportedError(
                f"The {self.driver_name} client of {self.connection_alias!r} reports no pool status"
            )
        acquire_count, acquire_timeouts, acquire_wait_seconds_total, connect_count, connect_failures = (
            self.pool_statistics.get_counts()
        )
        occupancy = self.get_pool_occupancy()
        if occupancy is None:
            if not connect_failures:
                return None
            # A pool failing to open - its failures are what there is to see.
            occupancy = (0, 0, 0, *self.get_pool_bounds())
        size, idle, waiting, min_size, max_size = occupancy
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

        Called before this client opens a connection. Build the command from its settings;
        fetch any external credentials needed by the command lazily within this hook.

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
        raise NotImplementedError()

    def get_pool_bounds(self) -> tuple[int, int]:
        """The least and the most connections the client's pool keeps, as configured - for
        ``get_pool_status()`` of a pool failing to open.

        Returns:
            The bounds.
        """
        raise NotImplementedError()

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
        raise NotImplementedError()

    async def _pool_acquire(self) -> Any:
        """
        Checks out one connection from the pool (PoolConnectionWrapper, a transaction's
        ``_take_transaction_resources()``) - only meaningful for a pooled client; a client of one
        connection never calls this.
        """
        raise NotImplementedError()

    async def acquire_from_open_pool(self) -> tuple[Any, Any]:
        """Checks out one connection of the pool - with the pool it came from.

        Returns:
            The pool and the connection.
        """
        return self._pool, await self._pool_acquire()

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
        raise NotImplementedError()

    async def db_create(self) -> None:
        """Creates the database on the server - for the test runner. Needs
        ``create_connection(with_db=False)`` first.
        """
        raise NotImplementedError()

    async def db_delete(self) -> None:
        """Drops the database on the server - for the test runner. Needs
        ``create_connection(with_db=False)`` first.
        """
        raise NotImplementedError()

    def acquire_connection(self) -> ConnectionWrapper[Any] | PoolConnectionWrapper[Any]:
        """
        Acquires a connection from the pool.
        Will return the current context connection if already in a transaction.
        """
        raise NotImplementedError()

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
        statements run one by one, each applied at once (``NonTransactionalContext``). Explicit
        transaction restrictions are refused: they cannot be enforced by such a block.

        Args:
            options: How the transaction runs - read-only, statement timeout, isolation level.

        Returns:
            The context.

        Raises:
            UnSupportedError: Transaction options are requested on a database without transactions.
        """
        if not self.features.supports_transactions:
            if options != TransactionOptions.DEFAULT:
                raise UnSupportedError(
                    f"{self.connection_alias!r} is a {self.dialect} database, which has no transactions"
                )
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
        raise NotImplementedError()

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

    #: Turns one named row of ``execute()`` into a dict, preserving exact column names and their values.
    row_to_dict: Callable[[Any], dict[str, Any]] = dict

    async def execute(
        self,
        query: str,
        values: list[Any] | None = None,
        *,
        returns_rows: bool | None = None,
        rows_by_position: bool = False,
        retry_read: bool = False,
        recovery: StatementRecovery | None = None,
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
            retry_read: Explicit permission to retry this invocation on connection loss outside
                a transaction. The caller guarantees it has no write effects; raw SQL defaults
                to False. Returning rows alone does not make a statement safe to retry.
            recovery: An explicit dialect failure action prepared by the ORM compiler.

        Returns:
            The affected or returned row count, and the rows.
        """
        raise NotImplementedError()

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
        raise NotImplementedError()

    async def execute_dicts(
        self, query: str, values: list[Any] | None = None, *, retry_read: bool = False
    ) -> list[dict[str, Any]]:
        """Executes a statement that returns rows, and returns them as dicts.

        Args:
            query: The SQL string, pre-parametrized for the target DB dialect.
            values: A sequence of positional DB parameters.
            retry_read: Permit retry of an explicitly safe read, as in ``execute()``. False by
                default; returning rows alone doesn't make user SQL safe to repeat.

        Returns:
            The rows.
        """
        return list(
            map(self.row_to_dict, (await self.execute(query, values, returns_rows=True, retry_read=retry_read)).rows)
        )

    async def execute_script(self, query: str, *, recovery: StatementRecovery | None = None) -> None:
        """
        Executes a RAW SQL script with multiple statements, and returns nothing.

        Args:
            query: The SQL string, which will be passed on verbatim.
                Semicolons is supported here.
            recovery: Failure metadata from an ORM compiler; absent for ordinary raw scripts.
        """
        raise NotImplementedError()

    async def invalidate_schema_cache(self) -> None:
        """Discard driver metadata after schema work, including a partially executed raw script.

        Drivers without schema-dependent metadata need no action. This hook neither repeats the
        completed statement nor inspects its SQL; schema executors call it at an explicit boundary.
        """

    def get_write_recovery(self, table: Table) -> StatementRecovery | None:
        """Compile dialect failure metadata from a write target; transactional dialects need none."""
        return None

    async def execute_built_script(self, query: str, *, recovery: StatementRecovery | None = None) -> None:
        """Executes a script hare built - its schema statements - as the connection runs them;
        ``execute_script()`` by default. A script the user wrote goes to ``execute_script()``.

        Args:
            query: The script.
            recovery: An explicit action for failures of this prepared target.
        """
        if recovery is None:
            await self.execute_script(query)
        else:
            await self.execute_script(query, recovery=recovery)

    def stream_batches(
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
        return StatementExecution.stream(self, query, values, chunk_size)

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
        yield  # type: ignore[unreachable]

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

    def get_shared_table_name(self, table: str) -> str:
        """The name a statement of the client reaches a table of hare's own bookkeeping by - kept in the
        connection's shared schema, not in a tenant's.

        Args:
            table: The table's name.

        Returns:
            The name - as it is by default.
        """
        return table

    async def get_generated_key_sql(self, model: type[Model]) -> str | None:
        """The SQL expression giving each row of a statement inserting a model's rows the next key of the
        model's series - for rows written by SQL of hare's own, such as rows carried over between tables.

        Args:
            model: The model.

        Returns:
            The expression; None when the database generates the keys as it writes the rows - by default.
        """
        return None

    async def synchronize_key_series(self, model: type[Model]) -> None:
        """Moves a model's series of keys past the greatest key its table holds - rows written with
        keys of their own leave the series behind. Nothing by default: a database generating keys as
        it writes the rows keeps up by itself.

        Args:
            model: The model.
        """

    async def execute_many(
        self, query: str, values: list[list[Any]], *, recovery: StatementRecovery | None = None
    ) -> None:
        """
        Executes one statement once per parameter row, and returns no data.

        Args:
            query: The SQL string, pre-parametrized for the target DB dialect.
            values: A sequence of positional DB parameters.
            recovery: Failure metadata shared by executions of the same prepared statement.
        """
        raise NotImplementedError()

    async def copy(
        self,
        table: str,
        columns: list[str],
        records: list[tuple[Any, ...]],
        column_types: list[str],
        schema: str | None = None,
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
            schema: The table's schema (``Meta.schema``) - None for the connection's default one.
        """
        raise NotImplementedError()

    async def open_across_closes(self) -> bool:
        """Opens the connection once (``create_connection()``), telling whether a close ran meanwhile
        (``Connections.reconnect()``) - then what opened belongs to no client: the caller closes it and
        opens anew.

        Returns:
            Whether a close ran while it opened.

        Raises:
            Exception: What the opening raised, unless a close ran meanwhile - its setup then broke on
                what the close took.
        """
        closes_before = self.closes_started
        self.opening_task = asyncio.current_task()
        try:
            await self.create_connection(with_db=True)
        except Exception:
            if self.closes_started == closes_before:
                raise
        finally:
            self.opening_task = None
        return self.closes_started != closes_before

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
            async with SchemaCacheInvalidation.run(self):
                await self.execute_built_script(schema)

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
