from __future__ import annotations

import asyncio
import contextvars
import operator
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from hare.dialects.base.results.described_result import DescribedResult
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient
from hare.dialects.postgresql.constants import (
    POSTGRES_AUTHORIZATION_SQLSTATE_CLASS,
    POSTGRES_SERVER_VERSION_NUMBER_COLUMN,
    POSTGRES_SERVER_VERSION_NUMBER_SQL,
)
from hare.dialects.postgresql.drivers.rust_pg.constants import (
    RUST_PG_DATABASE_ENVIRONMENT_VARIABLE,
    RUST_PG_DEFAULT_HOST,
    RUST_PG_EXTRA_KEYS,
    RUST_PG_HOST_ENVIRONMENT_VARIABLE,
    RUST_PG_MESSAGE_ENCODING_ERROR_MESSAGE,
    RUST_PG_PASSWORD_ENVIRONMENT_VARIABLE,
    RUST_PG_PROTOCOL_MAX_BIND_PARAMS,
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
from rust.native import pg

if TYPE_CHECKING:
    from hare.dialects.postgresql.drivers.rust_pg.client.rust_pg_transaction_client import RustPgTransactionClient


class RustPgClient(PostgresqlClient):
    row_to_dict = staticmethod(operator.methodcaller("to_dict"))
    driver_name = "postgresql"
    # rust.native.pg's COPY runs on a fresh pool connection of its own and commits there.
    copy_joins_transaction = False
    _pool: pg.Client | None
    _connection: pg.Client | None = None
    #: One permit per pool connection a transaction may pin: begin() takes one before any driver
    #: call - a cancellable wait in Python, not inside the driver's pool - and gives it back once
    #: the connection is in the pool again. None while there is no pool.
    _transaction_slots: asyncio.Semaphore | None = None
    # The driver's execute_many() sends one Sync per row and doesn't get cheaper per row as a batch
    # grows - bulk_create() sends one multi-row INSERT per chunk on this driver.
    features = PostgresqlClient.features.replace(execute_many_scales_poorly=True)
    # The driver's one connection failure type - a failed connect and a lost connection alike.
    RETRYABLE_CONNECT_EXCEPTIONS = (pg.ConnectionError,)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
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
            **{k: self.extra[k] for k in RUST_PG_EXTRA_KEYS if k in self.extra},
        }
        try:
            password = self.password
            if password is None:
                password = os.environ.get(RUST_PG_PASSWORD_ENVIRONMENT_VARIABLE)
            self._pool = await self._create_pool_with_retry(password=password, **self._template)
            self._transaction_slots = asyncio.Semaphore(max(1, int(self._template["max_size"])))
            await self._post_connect()
            self.log.debug("Created connection pool %s with params: %s", self._pool, self._template)
        except pg.InvalidCatalogError as ex:
            # A database that doesn't exist never appears on a retry - mirrors asyncpg.
            raise ConfigurationError(self._get_connection_failure_message(with_db, ex)) from None
        except pg.QueryError as ex:
            if not str(getattr(ex, "sqlstate", None) or "").startswith(POSTGRES_AUTHORIZATION_SQLSTATE_CLASS):
                raise
            # A rejected password or unknown role (SQLSTATE class 28) - retrying can't fix it.
            raise ConfigurationError(
                f"Authentication failed connecting to {self.database if with_db else 'default'} database "
                f"as user {self.user!r} - check the configured password/credentials. Exception: {ex}"
            ) from None
        except pg.ConnectionError as ex:
            raise DBConnectionError(self._get_connection_failure_message(with_db, ex)) from None

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
        except pg.ConnectionError as exc:
            raise DBConnectionError(str(exc)) from exc

    async def _expire_connections(self) -> None:
        if self._pool:  # pragma: nobranch
            await self._pool.expire_connections()

    async def _fetch_server_version_number(self) -> int:
        rows = await self._connected_pool.fetch_all(POSTGRES_SERVER_VERSION_NUMBER_SQL, [])
        return rows[0].to_dict()[POSTGRES_SERVER_VERSION_NUMBER_COLUMN]

    async def _close(self) -> None:
        if self._pool:  # pragma: nobranch
            pool = self._pool
            await pool.close()
            self._pool = None
            self._transaction_slots = None
            self.log.debug("Closed connection pool %s with params: %s", pool, self._template)

    def get_driver_error(self, error: Exception, call_arguments: tuple[Any, ...]) -> Exception | None:
        if isinstance(error, pg.IntegrityViolationError):
            return IntegrityError(error)
        # A statement reached a transaction whose COMMIT/ROLLBACK already took its connection - or
        # one in a state that refuses it.
        if isinstance(error, (pg.InvalidTransactionStateError, pg.TransactionFinishedError)):
            return TransactionManagementError(error)
        if isinstance(error, pg.ConnectionError):
            return self.get_connection_error_translation(error, call_arguments)
        if isinstance(error, (pg.QueryError, pg.ConversionError)):
            # A serialization failure or a deadlock (SQLSTATE 40001/40P01) is a TransactionRetryError.
            return self._get_operational_error(error)
        # pg.InvalidCatalogError isn't translated - db_delete() handles it.
        return None

    @staticmethod
    def get_connection_error_translation(exc: Exception, call_arguments: tuple[Any, ...]) -> Exception:
        """The hare exception for a ``pg.ConnectionError`` - a statement tokio-postgres failed to
        encode never left the client, so it is a query error, not a lost connection.

        Args:
            exc: The driver's exception.
            call_arguments: The failed call's positional arguments (query text, bind values).

        Returns:
            TooManyParametersError for an encoding failure of a statement binding more parameters
            than the protocol carries, OperationalError for any other encoding failure,
            DBConnectionError otherwise.
        """
        if RUST_PG_MESSAGE_ENCODING_ERROR_MESSAGE not in str(exc):
            return DBConnectionError(exc)
        if any(
            isinstance(argument, (list, tuple)) and len(argument) > RUST_PG_PROTOCOL_MAX_BIND_PARAMS
            for argument in call_arguments
        ):
            return TooManyParametersError(exc)
        return OperationalError(exc)

    async def db_delete(self) -> None:
        try:
            return await super().db_delete()
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
        self.log.debug("%s: %s", query, values)
        await self._run_statement(lambda executor: executor.execute_many(query, values))

    @PostgresqlClient.translate_exceptions
    async def copy(
        self, table: str, columns: list[str], records: list[tuple[Any, ...]], column_types: list[str]
    ) -> None:
        """Loads ``records`` into ``table`` through the COPY protocol, on a pool connection of its own
        - the driver has no COPY inside a transaction, so it never joins a surrounding
        ``Transactions.atomic()``. Only hare's core scalar field types: COPY BINARY needs each
        column's type declared.
        """
        await self._ensure_connection()
        self.log.debug("COPY %s(%s): %d record(s)", table, columns, len(records))
        await self._connected_pool.copy_in(table, columns, column_types, [list(row) for row in records])

    @PostgresqlClient.translate_exceptions
    async def execute(
        self, query: str, values: list[Any] | None = None, *, returns_rows: bool | None = None
    ) -> StatementResult:
        values = values or []
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
        self.log.debug(query)
        await self._run_statement(lambda executor: executor.execute_script(query))
