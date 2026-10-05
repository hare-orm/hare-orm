from __future__ import annotations

import asyncio
import contextvars
import logging
import operator
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, ClassVar

from rust.native.pool import PoolStatistics, set_pool_metrics_enabled

from hare.dialects.base.client.connect_failure_reports import ConnectFailureReports
from hare.dialects.base.client.pool.pool_timeouts import PoolTimeouts
from hare.dialects.base.results.described_result import DescribedResult
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.postgresql.client.constants import POSTGRES_PASSWORD_ENVIRONMENT_VARIABLE
from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient
from hare.dialects.postgresql.client.postgresql_connect_errors import PostgresqlConnectErrors
from hare.dialects.postgresql.client.transaction_pooler import TransactionPooler
from hare.dialects.postgresql.constants import (
    POSTGRES_SERVER_VERSION_NUMBER_COLUMN,
    POSTGRES_SERVER_VERSION_NUMBER_SQL,
)
from hare.dialects.postgresql.drivers.constants import (
    POSTGRES_SHELL_SSL_MODE_VARIABLE,
    POSTGRES_SHELL_SSL_ROOT_CERT_VARIABLE,
    POSTGRESQL_STALE_PLAN_MESSAGE,
    POSTGRESQL_STALE_PLAN_SQLSTATE,
)
from hare.dialects.postgresql.drivers.rust_pg.constants import (
    RUST_PG_DATABASE_ENVIRONMENT_VARIABLE,
    RUST_PG_DEFAULT_HOST,
    RUST_PG_EXTRA_KEYS,
    RUST_PG_HOST_ENVIRONMENT_VARIABLE,
    RUST_PG_MESSAGE_ENCODING_ERROR_MESSAGE,
    RUST_PG_PROTOCOL_MAX_BIND_PARAMETERS,
    RUST_PG_USER_ENVIRONMENT_VARIABLE,
)
from hare.exceptions import (
    ConfigurationError,
    DBConnectionError,
    IntegrityError,
    OperationalError,
    TooManyParametersError,
    TransactionManagementError,
)
from hare.instrumentation.constants import DURATION_RECORDS_CAPACITY
from hare.instrumentation.pools.pool_metrics import PoolMetrics
from rust.native import pg

if TYPE_CHECKING:
    from hare.dialects.postgresql.drivers.rust_pg.client.rust_pg_transaction_client import RustPgTransactionClient


class RustPgClient(PostgresqlClient):
    row_to_dict = staticmethod(operator.methodcaller("to_dict"))
    driver_name = "postgresql"
    _pool: pg.Client | None
    _connection: pg.Client | None = None
    #: One permit per pool connection a transaction may pin: begin() takes one before any driver
    #: call - a cancellable wait in Python, not inside the driver's pool - and gives it back once
    #: the connection is in the pool again. None while there is no pool.
    _transaction_slots: asyncio.Semaphore | None = None
    # The driver's execute_many() sends one Sync per row and doesn't get cheaper per row as a batch
    # grows - bulk_create() sends one multi-row INSERT per chunk on this driver.
    features = PostgresqlClient.features.replace(execute_many_scales_poorly=True, binds_written_parameters=True)
    # The driver's one connection failure type - a failed connect and a lost connection alike.
    RETRYABLE_CONNECT_EXCEPTIONS = (pg.ConnectionError,)
    #: Whether the driver follows ``PoolMetrics`` - told once, by the first client made.
    follows_pool_metrics: ClassVar[bool] = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not RustPgClient.follows_pool_metrics:
            # The driver measures its waits itself - on PoolMetrics' word.
            PoolMetrics.add_switch(set_pool_metrics_enabled)
            RustPgClient.follows_pool_metrics = True
        #: The transactions waiting for a transaction slot - waiting for a pool connection, outside
        #: the driver's own pool.
        self.transaction_slot_waiting = 0
        # Dropping an SSL setting silently would connect without the TLS the caller asked for.
        misspelled_ssl_keys = [key for key in ("ssl", "sslmode") if key in self.extra]
        if misspelled_ssl_keys:
            raise ConfigurationError(
                f"The rust Postgres driver does not accept {misspelled_ssl_keys!r} - use ssl_mode="
                "disable/prefer/require/verify-ca/verify-full instead"
            )
        # This driver has no generic passthrough - a parameter it doesn't know would be dropped
        # silently, the same typo a DB_URL query parameter is already rejected for.
        unknown_parameters = sorted(set(self.extra) - set(RUST_PG_EXTRA_KEYS))
        if unknown_parameters:
            raise ConfigurationError(
                f"Unknown connection parameter(s) {unknown_parameters!r} for the rust Postgres driver: "
                f"expected one of {sorted(RUST_PG_EXTRA_KEYS)} besides the common Postgres parameters"
            )

    @staticmethod
    def get_os_user() -> str:
        """The operating system user - the user a connection falls back to, like libpq.

        Returns:
            The user name.
        """
        # Local import: getpass costs almost a millisecond to import, and a connection naming its
        # user never needs it.
        import getpass

        return getpass.getuser()

    def create_pool_statistics(self) -> Any:
        # The pool lives in the driver, which counts in this object - kept across the client's pools.
        return PoolStatistics(DURATION_RECORDS_CAPACITY)

    def get_pool_occupancy(self) -> tuple[int, int, int, int, int] | None:
        pool = self._pool
        if pool is None:
            return None
        size, idle, waiting, max_size = pool.get_pool_status()
        return size, idle, waiting + self.transaction_slot_waiting, self.pool_minsize, max_size

    async def create_connection(self, with_db: bool) -> None:
        if self.schema:
            self.server_settings["search_path"] = self.schema

        # Unset connection settings fall back the way libpq (and so asyncpg) resolves them - the
        # PG* environment variables, then the OS user and localhost - since the driver itself
        # reads no environment.
        self._template = {
            "host": self.host or os.environ.get(RUST_PG_HOST_ENVIRONMENT_VARIABLE) or RUST_PG_DEFAULT_HOST,
            "port": self.port,
            "user": self.user or os.environ.get(RUST_PG_USER_ENVIRONMENT_VARIABLE) or self.get_os_user(),
            "database": (self.database or os.environ.get(RUST_PG_DATABASE_ENVIRONMENT_VARIABLE)) if with_db else None,
            "min_size": self.pool_minsize,
            "max_size": self.pool_maxsize,
            "application_name": self.application_name,
            "server_settings": self.server_settings or None,
            "pool_acquire_timeout": self.pool_acquire_timeout,
            "statistics": self.pool_statistics,
            **{key: self.extra[key] for key in RUST_PG_EXTRA_KEYS if key in self.extra},
        }
        try:
            password = self.password
            if self.password_provider is not None:
                password = await self.password_provider.get()
            elif password is None:
                password = os.environ.get(POSTGRES_PASSWORD_ENVIRONMENT_VARIABLE)
            self._pool = await self._create_pool_with_retry(password=password, **self._template)
            if self.password_provider is not None:
                # The driver opens the pool's connections itself - it gets each new password.
                self.password_provider.start_refreshing(self.apply_password)
            self._transaction_slots = asyncio.Semaphore(max(1, int(self._template["max_size"])))
            await self._post_connect()
            self.log.debug("Created connection pool %s with params: %s", self._pool, self._template)
        except Exception as error:
            connect_error = PostgresqlConnectErrors.get_error(self, error, with_db)
            if connect_error is None:
                raise
            raise connect_error from None

    async def create_pool(self, **kwargs: Any) -> pg.Client:
        return await pg.connect(**kwargs)

    async def _pool_acquire(self) -> Any:
        """Takes a connection from the pool. The acquire timeout is part of the pool itself; a timeout
        and a connection failure both raise ``pg.ConnectionError``, translated to
        ``DBConnectionError``.
        """
        assert self._pool is not None, "_pool_acquire called without create_connection() first"  # nosec B101
        # An idle connection is checked out on the spot, without a trip to the tokio runtime and
        # back through the event loop; only a wait for one (or a new one) is awaited.
        try:
            if (connection := self._pool.try_acquire()) is not None:
                return connection
            return await self._pool.acquire()
        except pg.PoolTimeoutError:
            raise PoolTimeouts.get_error(self, self.pool_acquire_timeout or 0.0, counted=True) from None
        except pg.ConnectionError as error:
            if isinstance(error, pg.ConnectFailedError):
                ConnectFailureReports.record(self, error, counted=True)
            raise DBConnectionError(str(error)) from error

    async def _expire_connections(self) -> None:
        if self._pool:  # pragma: nobranch
            await self._pool.expire_connections()

    async def _fetch_server_version_number(self) -> int:
        rows = await self._connected_pool.fetch_all(POSTGRES_SERVER_VERSION_NUMBER_SQL, [])
        return rows[0].to_dict()[POSTGRES_SERVER_VERSION_NUMBER_COLUMN]

    def get_shell_ssl_environment(self) -> dict[str, str]:
        environment = {}
        if self.extra.get("ssl_mode"):
            environment[POSTGRES_SHELL_SSL_MODE_VARIABLE] = self.extra["ssl_mode"]
        if self.extra.get("ssl_root_cert"):
            environment[POSTGRES_SHELL_SSL_ROOT_CERT_VARIABLE] = self.extra["ssl_root_cert"]
        return environment

    def apply_password(self, password: str) -> None:
        if self._pool is not None:
            self._pool.set_password(password)

    async def _close(self) -> None:
        if self.password_provider is not None:
            await self.password_provider.stop_refreshing()
        if self._pool:  # pragma: nobranch
            pool = self._pool
            await pool.close()
            self._pool = None
            self._transaction_slots = None
            self.log.debug("Closed connection pool %s with params: %s", pool, self._template)

    @staticmethod
    def is_missing_database_error(error: BaseException) -> bool:
        return isinstance(error, pg.InvalidCatalogError)

    @staticmethod
    def is_stale_plan_error(error: Exception) -> bool:
        return (
            isinstance(error, pg.QueryError)
            and getattr(error, "sqlstate", None) == POSTGRESQL_STALE_PLAN_SQLSTATE
            and POSTGRESQL_STALE_PLAN_MESSAGE in str(error)
        )

    def get_driver_error(self, error: Exception, call_arguments: tuple[Any, ...]) -> Exception | None:
        if isinstance(error, pg.IntegrityViolationError):
            return IntegrityError(error)
        # A statement reached a transaction whose COMMIT/ROLLBACK already took its connection - or
        # one in a state that refuses it.
        if isinstance(error, (pg.InvalidTransactionStateError, pg.TransactionFinishedError)):
            return TransactionManagementError(error)
        if isinstance(error, pg.PoolTimeoutError):
            return PoolTimeouts.get_error(self, self.pool_acquire_timeout or 0.0, counted=True)
        if isinstance(error, pg.ConnectionError):
            if isinstance(error, pg.ConnectFailedError):
                # The pool grew inside the driver and its new connection failed to open.
                ConnectFailureReports.record(self, error, counted=True)
            return self.get_connection_error_translation(error, call_arguments)
        if isinstance(error, (pg.QueryError, pg.ConversionError)):
            # A serialization failure or a deadlock (SQLSTATE 40001/40P01) is a TransactionRetryError.
            return self._get_operational_error(error)
        # pg.InvalidCatalogError isn't translated - db_delete() handles it.
        return None

    @staticmethod
    def get_connection_error_translation(error: Exception, call_arguments: tuple[Any, ...]) -> Exception:
        """The hare exception for a ``pg.ConnectionError`` - a statement tokio-postgres failed to
        encode never left the client, so it is a query error, not a lost connection.

        Args:
            error: The driver's exception.
            call_arguments: The failed call's positional arguments (query text, bind values).

        Returns:
            TooManyParametersError for an encoding failure of a statement binding more parameters
            than the protocol carries, OperationalError for any other encoding failure,
            DBConnectionError otherwise.
        """
        if RUST_PG_MESSAGE_ENCODING_ERROR_MESSAGE not in str(error):
            return DBConnectionError(error)
        if any(
            isinstance(argument, (list, tuple, pg.PgParameters, pg.PgParameterRows))
            and len(argument) > RUST_PG_PROTOCOL_MAX_BIND_PARAMETERS
            for argument in call_arguments
        ):
            return TooManyParametersError(error)
        return OperationalError(error)

    async def db_delete(self) -> None:
        try:
            await super().db_delete()
            return
        except pg.InvalidCatalogError:  # pragma: nocoverage
            pass
        await self.close()

    def _get_transaction_client(self) -> RustPgTransactionClient:
        # Imported here: the modules import each other.
        from hare.dialects.postgresql.drivers.rust_pg.client.rust_pg_transaction_client import RustPgTransactionClient

        return RustPgTransactionClient(self)

    async def _run_statement(self, make_call: Callable[[Any], Awaitable[Any]]) -> Any:
        """Runs one statement on the pool, created on first use.

        Args:
            make_call: Starts the statement on the executor it is given - called only once the
                statement may run: rust.native.pg sends a query the moment its method is called.

        Returns:
            What the statement returned.
        """
        pool = self._pool
        if pool is None:
            await self._ensure_connection()
            pool = self._connected_pool
        return await make_call(pool)

    @property
    def _connected_pool(self) -> pg.Client:
        """Narrows `self._pool: pg.Client | None` - only ever called right after
        `_ensure_connection()` has run, which guarantees it's set."""
        assert self._pool is not None, "_connected_pool accessed without _ensure_connection() first"  # nosec B101
        return self._pool

    async def listen(self, channel: str, callback: Callable[[pg.Listener, int, str, str], None]) -> pg.Listener:
        """Opens a dedicated connection, outside the pool, and LISTENs on ``channel``, calling
        ``callback(connection, pid, channel, payload)`` on the event loop's thread for every NOTIFY.
        The caller owns the returned listener and closes it; ``is_closed()`` is also true once the
        connection dies. For a subscription that reconnects use
        ``hare.contrib.notify.NotificationListener``.
        """
        if self.transaction_pooling:
            # A LISTEN holds its session - on the server itself, past the transaction pooler.
            return await TransactionPooler.get_direct_client(self).listen(channel, callback)
        await self._ensure_connection()
        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()

        def dispatch_to_loop(listener: pg.Listener, pid: int, channel_name: str, payload: str) -> None:
            try:
                loop.call_soon_threadsafe(callback, listener, pid, channel_name, payload, context=context)
            except RuntimeError:
                # The loop that opened this listener is already closed - nobody left to deliver to.
                return

        return await self._connected_pool.listen(channel, dispatch_to_loop)

    @PostgresqlClient.translate_exceptions
    async def execute_many(self, query: str, values: list[Any]) -> None:
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("%s: %s", query, values)
        await self._run_statement(lambda executor: executor.execute_many(query, values))

    @PostgresqlClient.translate_exceptions
    async def copy(
        self, table: str, columns: list[str], records: list[tuple[Any, ...]], column_types: list[str]
    ) -> None:
        """Loads ``records`` into ``table`` through the COPY protocol, on a pool connection of its own
        - a transaction's client runs it on the transaction's connection. Only hare's core scalar
        field types: COPY BINARY needs each column's type declared.
        """
        await self._ensure_connection()
        self.log.debug("COPY %s(%s): %d record(s)", table, columns, len(records))
        await self._connected_pool.copy_in(table, columns, column_types, [list(row) for row in records])

    @PostgresqlClient.translate_exceptions
    async def execute(
        self,
        query: str,
        values: list[Any] | None = None,
        *,
        returns_rows: bool | None = None,
        rows_by_position: bool = False,
    ) -> StatementResult:
        values = values or []
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("%s: %s", query, values)
        if returns_rows is None:
            returns_rows = not self._is_write_without_returning(query)
        if not returns_rows:
            rows_affected = await self._run_statement(lambda executor: executor.execute(query, values))
            return StatementResult(rows_affected, [])
        pool = self._pool
        if pool is not None and not self.is_transaction_client:
            # Straight on the pool - _run_statement()'s path outside a transaction.
            rows = await pool.fetch_all(query, values)
        else:
            rows = await self._run_statement(lambda executor: executor.fetch_all(query, values))
        return StatementResult(len(rows), rows)

    @PostgresqlClient.translate_exceptions
    async def execute_described(self, query: str, values: list[Any] | None = None) -> DescribedResult:
        values = values or []
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("%s: %s", query, values)
        if self._is_write_without_returning(query):
            rows_affected = await self._run_statement(lambda executor: executor.execute(query, values))
            return DescribedResult(columns=(), rows=(), row_count=rows_affected)
        columns, rows = await self._run_statement(lambda executor: executor.fetch_all_described(query, values))
        return DescribedResult(
            columns=tuple(columns), rows=tuple(tuple(row.values()) for row in rows), row_count=len(rows)
        )

    @PostgresqlClient.translate_exceptions
    async def execute_script(self, query: str) -> None:
        # In a transaction, on its own pinned connection - another pool connection would wait
        # forever on a lock the transaction holds (a TRUNCATE ... CASCADE).
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug(query)
        await self._run_statement(lambda executor: executor.execute_script(query))
