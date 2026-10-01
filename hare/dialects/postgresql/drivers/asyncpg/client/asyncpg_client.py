import asyncio
import contextlib
import datetime
import decimal
import functools
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

import asyncpg

from hare.dialects.base.results.described_result import DescribedResult
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient
from hare.dialects.postgresql.constants import (
    POSTGRES_SERVER_VERSION_NUMBER_SQL,
)
from hare.dialects.postgresql.drivers.asyncpg.codecs import AsyncpgTimestampCodec
from hare.dialects.postgresql.drivers.asyncpg.constants import (
    ASYNCPG_CONNECTION_OPTIONS,
    ASYNCPG_CONNECTION_PARAMETERS,
    ASYNCPG_POOL_ONLY_PARAMETERS,
    ASYNCPG_TOO_MANY_ARGUMENTS_MESSAGE,
    EPOCH_YEAR,
    FAST_SCALAR_BIND_TYPES,
    PLAIN_BIND_TYPE_TUPLE,
    PLAIN_BIND_TYPES,
    POOL_CLOSE_TIMEOUT_SECONDS,
)
from hare.exceptions import (
    ConfigurationError,
    DBConnectionError,
    IntegrityError,
    OperationalError,
    TooManyParametersError,
    TransactionManagementError,
)
from hare.fields.constants import NAIVE_INFINITY_DATETIMES
from hare.utils import Timezone
from hare.utils.native_modules import NativeModules

if TYPE_CHECKING:
    from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_transaction_client import AsyncpgTransactionClient


class AsyncpgClient(PostgresqlClient):
    driver_name = "postgresql+asyncpg"
    # asyncpg decodes a whole numeric ending in zero digit groups with a positive exponent
    # (100000 as Decimal('1.0E+5')) - a DecimalField value is quantized to the field's scale.
    native_python_types = PostgresqlClient.native_python_types - {decimal.Decimal}
    connection_class = asyncpg.connection.Connection
    #: The compiled ``rust.native.rows`` - checks a row's value types in one call; None where it
    #: isn't built.
    native_rows: ClassVar[Any] = NativeModules.rows
    _pool: asyncpg.Pool | None
    _connection: asyncpg.connection.Connection | None = None
    # What a connect is retried on: OSError (refused, DNS), a connection lost at the protocol level,
    # and SQLSTATE 57P01-57P04 - the server starting up, shutting down or recovering. Not a missing
    # database.
    RETRYABLE_CONNECT_EXCEPTIONS = (
        OSError,
        asyncpg.exceptions.PostgresConnectionError,
        asyncpg.exceptions.AdminShutdownError,
        asyncpg.exceptions.CrashShutdownError,
        asyncpg.exceptions.CannotConnectNowError,
        asyncpg.exceptions.DatabaseDroppedError,
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        unknown_parameters = sorted(set(self.extra) - ASYNCPG_CONNECTION_PARAMETERS)
        if unknown_parameters:
            raise ConfigurationError(
                f"Unknown connection parameter(s) {unknown_parameters!r} for the asyncpg Postgres driver"
            )
        self.extra.update(ASYNCPG_CONNECTION_OPTIONS.read(self.extra))

    @classmethod
    def _asyncpg_bind_value(cls, value: Any) -> Any:
        """Adapts a value for asyncpg: a ``Range`` becomes an ``asyncpg.Range``, the only type its
        range codec takes; a naive ``datetime.time`` gets a UTC offset - a ``TimeField`` column is
        ``TIMETZ``, and the codec rejects a naive time.
        """
        if type(value) in FAST_SCALAR_BIND_TYPES:
            if type(value) is datetime.time and value.tzinfo is None:
                return value.replace(tzinfo=datetime.UTC)
            if (
                type(value) is datetime.datetime
                and value.tzinfo is None
                and value.year < EPOCH_YEAR
                and value not in NAIVE_INFINITY_DATETIMES
            ):
                return cls._get_bindable_pre_epoch_datetime(value)
            return value
        if type(value) is list:
            # Elements are adapted like scalars - a TIMETZ[] parameter (a large TimeField __in list)
            # needs the naive-time offset, an ArrayField(RangeField) value asyncpg.Range elements,
            # a nested (multi-dimensional) list the same at every level.
            for element in value:
                element_type = type(element)
                if (
                    element_type not in FAST_SCALAR_BIND_TYPES
                    or element_type is datetime.time
                    or element_type is datetime.datetime
                ):
                    return [cls._asyncpg_bind_value(element) for element in value]
            return value
        if (
            hasattr(value, "lower_inc")
            and hasattr(value, "upper_inc")
            and hasattr(value, "lower")
            and hasattr(value, "upper")
        ):
            # empty= is passed on: an empty range has the same None bounds as an unbounded one.
            return asyncpg.Range(
                value.lower,
                value.upper,
                lower_inc=value.lower_inc,
                upper_inc=value.upper_inc,
                empty=getattr(value, "is_empty", False),
            )
        return value

    @staticmethod
    def _get_bindable_pre_epoch_datetime(value: datetime.datetime) -> datetime.datetime:
        """A naive pre-1970 datetime asyncpg can bind to a TIMESTAMPTZ.

        asyncpg reads a naive value as the system's local time through ``datetime.astimezone()``,
        which fails before 1970 on Windows - such a value gets the system zone attached here.

        Args:
            value: A naive datetime before 1970.

        Returns:
            `value` itself when ``astimezone()`` can convert it, the aware local value otherwise.
        """
        try:
            value.astimezone()
        except OSError, OverflowError, ValueError:
            return Timezone.make_system_local_aware(value)
        return value

    @classmethod
    def _asyncpg_bind_values(cls, values: Sequence[Any]) -> list[Any]:
        """``values`` adapted for asyncpg by ``_asyncpg_bind_value()`` - a row of plain scalars
        (``PLAIN_BIND_TYPES``, the usual case) as it is, checked without a call per value.

        Args:
            values: The statement's parameters.

        Returns:
            The parameters to bind.
        """
        if cls.native_rows is not None and type(values) is list:
            if cls.native_rows.are_all_of_types(values, PLAIN_BIND_TYPE_TUPLE):
                return values
            return [cls._asyncpg_bind_value(value) for value in values]
        for value in values:
            if type(value) not in PLAIN_BIND_TYPES:
                return [cls._asyncpg_bind_value(value) for value in values]
        return values if type(values) is list else list(values)

    async def create_connection(self, with_db: bool) -> None:
        if self.schema:
            self.server_settings["search_path"] = self.schema

        if self.application_name:
            self.server_settings["application_name"] = self.application_name

        self._template = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "database": self.database if with_db else None,
            "min_size": self.pool_minsize,
            "max_size": self.pool_maxsize,
            "connection_class": self.connection_class,
            "loop": self.loop,
            "server_settings": self.server_settings,
            **self.extra,
        }
        try:
            self._pool = await self._create_pool_with_retry(password=self.password, **self._template)
            await self._post_connect()
            self.log.debug("Created connection pool %s with params: %s", self._pool, self._template)
        except asyncpg.InvalidAuthorizationSpecificationError as ex:
            # A class 28 authentication failure is a ConfigurationError, not a DBConnectionError -
            # retrying can't fix a bad credential.
            raise ConfigurationError(
                f"Authentication failed connecting to {self.database if with_db else 'default'} database "
                f"as user {self.user!r} - check the configured password/credentials. Exception: {ex}"
            )
        except TypeError as ex:
            # asyncpg rejects an unknown connection kwarg with a TypeError - a ConfigurationError,
            # as retrying can't fix it.
            raise ConfigurationError(
                f"Can't create a connection pool for {self.database if with_db else 'default'} database - "
                f"one of the configured connection parameters is invalid. Exception: {ex}"
            )
        except asyncpg.InvalidCatalogNameError as ex:
            # A database that doesn't exist never appears on a retry - a configuration problem,
            # like the authentication failure above.
            raise ConfigurationError(self._get_connection_failure_message(with_db, ex)) from None
        except self.RETRYABLE_CONNECT_EXCEPTIONS as ex:
            # What the retries re-raise once they are used up - a DBConnectionError.
            raise DBConnectionError(self._get_connection_failure_message(with_db, ex)) from None

    @staticmethod
    async def _skip_default_reset(connection: asyncpg.Connection) -> None:
        """Replaces asyncpg's reset on release (``pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *;
        RESET ALL``) with nothing: hare takes no advisory lock, holdable cursor, LISTEN or
        session-level SET on a pool connection, and the reset costs a round trip per release.
        asyncpg still rolls back an open transaction before this runs.
        """

    async def create_pool(self, **kwargs) -> asyncpg.Pool:
        configured_init = kwargs.pop("init", None)
        return await asyncpg.create_pool(
            None,
            reset=self._skip_default_reset,
            init=functools.partial(self._init_connection, configured_init),
            **kwargs,
        )

    @staticmethod
    async def _init_connection(
        configured_init: Callable[[asyncpg.Connection], Any] | None, connection: asyncpg.Connection
    ) -> None:
        """Prepares a new pool connection: hare's codecs, then the configured ``init`` callback.

        Args:
            configured_init: The ``init`` connection parameter, if any.
            connection: The new connection.
        """
        await AsyncpgTimestampCodec.install(connection)
        if configured_init is not None:
            await configured_init(connection)

    async def listen(
        self, channel: str, callback: Callable[[asyncpg.Connection, int, str, str], None]
    ) -> asyncpg.Connection:
        """Opens a dedicated connection, outside the pool, and LISTENs on ``channel``, calling
        ``callback`` for every NOTIFY. The caller owns the returned connection and closes it. For a
        subscription that reconnects use ``hare.contrib.notify.NotificationListener``.
        """
        if self.schema:
            # listen() may run before the pool exists - the schema's search_path is set here too.
            self.server_settings["search_path"] = self.schema
        connect_parameters = {
            key: value for key, value in self.extra.items() if key not in ASYNCPG_POOL_ONLY_PARAMETERS
        }
        try:
            connection = await asyncpg.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                connection_class=self.connection_class,
                loop=self.loop,
                server_settings=self.server_settings,
                **connect_parameters,
            )
        except TypeError as ex:
            raise ConfigurationError(
                f"Can't open a LISTEN connection to {self.database} database - one of the configured "
                f"connection parameters is invalid. Exception: {ex}"
            ) from ex
        except asyncpg.InvalidAuthorizationSpecificationError as ex:
            # The same mapping as create_connection() - this connection is opened directly.
            raise ConfigurationError(
                f"Authentication failed connecting to {self.database} database as user "
                f"{self.user!r} - check the configured password/credentials. Exception: {ex}"
            )
        except asyncpg.InvalidCatalogNameError as ex:
            raise ConfigurationError(self._get_connection_failure_message(True, ex)) from None
        except self.RETRYABLE_CONNECT_EXCEPTIONS as ex:
            raise DBConnectionError(self._get_connection_failure_message(True, ex)) from None
        await connection.add_listener(channel, callback)
        return connection

    async def _pool_acquire(self) -> asyncpg.pool.PoolConnectionProxy:
        """Takes a connection from the pool, waiting at most ``pool_acquire_timeout`` - indefinitely
        when it isn't set.
        """
        assert self._pool is not None, "_pool_acquire called without create_connection() first"  # nosec B101
        try:
            return await self._pool.acquire(timeout=self.pool_acquire_timeout)
        except TimeoutError as exc:
            raise DBConnectionError(
                f"Pool exhausted: timed out waiting {self.pool_acquire_timeout}s for a connection"
            ) from exc

    async def _pool_release(self, pool: asyncpg.Pool, connection: asyncpg.pool.PoolConnectionProxy) -> None:
        """Hands the connection back to ``pool``, or terminates it when the pool can't take it back - a
        terminated pool, or a connection the server closed while it was checked out, whose slot
        asyncpg wouldn't free.

        Args:
            pool: The pool the connection was acquired from.
            connection: The connection to hand back.
        """
        try:
            # The pool keeps a closed connection's slot taken - asyncpg frees it on terminate().
            closed = connection.is_closed()
        except asyncpg.InterfaceError:
            # Released already - the proxy is detached.
            return
        try:
            await pool.release(connection)
        except asyncpg.InterfaceError:
            # A terminated pool, or one the connection isn't of.
            closed = True
        if closed:
            with contextlib.suppress(asyncpg.InterfaceError):
                connection.terminate()

    async def _expire_connections(self) -> None:
        if self._pool:  # pragma: nobranch
            await self._pool.expire_connections()

    async def _fetch_server_version_number(self) -> int:
        async with self.acquire_connection() as connection:
            return await connection.fetchval(POSTGRES_SERVER_VERSION_NUMBER_SQL)

    async def _close(self) -> None:
        if self._pool:  # pragma: nobranch
            pool = self._pool
            try:
                try:
                    await asyncio.wait_for(pool.close(), POOL_CLOSE_TIMEOUT_SECONDS)
                except TimeoutError:  # pragma: nocoverage
                    pool.terminate()
            finally:
                # Clear the reference even if close()/terminate() itself raised something other
                # than TimeoutError - leaving self._pool pointing at a pool that failed to close
                # would make _expire_connections() and a future _close() treat it as still live.
                self._pool = None
            self.log.debug("Closed connection pool %s with params: %s", pool, self._template)

    def get_driver_error(self, error: Exception, call_arguments: tuple[Any, ...]) -> Exception | None:
        if isinstance(error, (asyncpg.SyntaxOrAccessError, asyncpg.exceptions.DataError)):
            return OperationalError(error)
        if isinstance(error, asyncpg.IntegrityConstraintViolationError):
            return IntegrityError(error)
        if isinstance(error, asyncpg.InvalidTransactionStateError):  # pragma: nocoverage
            return TransactionManagementError(error)
        if isinstance(error, asyncpg.InterfaceError):
            # A client-side refusal to send a statement with too many arguments, not a lost
            # connection - every other InterfaceError is one.
            if str(error).startswith(ASYNCPG_TOO_MANY_ARGUMENTS_MESSAGE):
                return TooManyParametersError(error)
            return DBConnectionError(error)
        # A lost connection - the server's PostgresConnectionError, a client-side InterfaceError, or
        # the protocol state machine desynchronized by a dropped connection.
        if isinstance(error, (asyncpg.exceptions.PostgresConnectionError, asyncpg.exceptions.InternalClientError)):
            return DBConnectionError(error)
        # SQLSTATE 57P01-57P04 - the server going down, crashing, rejecting work, the database
        # dropped: the connection is unusable. Checked before the broader classes below, which share
        # their base class. TooManyConnectionsError is connection exhaustion.
        if isinstance(
            error,
            (
                asyncpg.exceptions.AdminShutdownError,
                asyncpg.exceptions.CrashShutdownError,
                asyncpg.exceptions.CannotConnectNowError,
                asyncpg.exceptions.DatabaseDroppedError,
                asyncpg.exceptions.TooManyConnectionsError,
            ),
        ):
            return DBConnectionError(error)
        # A cancelled query (statement or lock timeout), a NOWAIT lock failure and the class 40
        # rollbacks - a serialization failure and a deadlock become TransactionRetryError, the rest
        # OperationalError.
        if isinstance(
            error,
            (
                asyncpg.exceptions.OperatorInterventionError,
                asyncpg.exceptions.LockNotAvailableError,
                asyncpg.exceptions.TransactionRollbackError,
            ),
        ):
            return self._get_operational_error(error)
        # Class 55 (an object in use - DROP DATABASE with open sessions), class XX (an internal
        # server error) and class 0A (a feature not supported) - and every other Postgres error -
        # are an OperationalError.
        if isinstance(error, asyncpg.PostgresError):
            return OperationalError(error)
        return None

    async def db_delete(self) -> None:
        try:
            return await super().db_delete()
        except asyncpg.InvalidCatalogNameError:  # pragma: nocoverage
            pass
        except OperationalError as exc:
            # A missing database arrives wrapped in OperationalError - the original exception is its
            # __context__.
            if not isinstance(exc.__context__, asyncpg.InvalidCatalogNameError):
                raise
        await self.close()

    def _get_transaction_client(self) -> AsyncpgTransactionClient:
        # Imported here: the modules import each other.
        from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_transaction_client import AsyncpgTransactionClient

        return AsyncpgTransactionClient(self)

    @PostgresqlClient.translate_exceptions
    async def execute_many(self, query: str, values: list[Any]) -> None:
        async with self.acquire_connection() as connection:
            self.log.debug("%s: %s", query, values)
            transaction = connection.transaction()
            await transaction.start()
            try:
                await connection.executemany(query, [self._asyncpg_bind_values(row) for row in values])
            except Exception:
                await transaction.rollback()
                raise
            else:
                await transaction.commit()

    @PostgresqlClient.translate_exceptions
    async def copy(
        self, table: str, columns: list[str], records: list[tuple[Any, ...]], column_types: list[str]
    ) -> None:
        """Loads ``records`` into ``table`` through the COPY protocol (``copy_records_to_table``) - no
        ON CONFLICT, no RETURNING. Runs on the connection ``acquire_connection()`` gives, so inside
        a transaction it is part of it. ``column_types`` is unused: asyncpg reads the column types
        itself.
        """
        async with self.acquire_connection() as connection:
            self.log.debug("COPY %s(%s): %d record(s)", table, columns, len(records))
            await connection.copy_records_to_table(
                table,
                records=[self._asyncpg_bind_values(row) for row in records],
                columns=columns,
                schema_name=self.schema,
            )

    @PostgresqlClient.translate_exceptions
    async def execute(
        self, query: str, values: list[Any] | None = None, *, returns_rows: bool | None = None
    ) -> StatementResult:
        async with self.acquire_connection() as connection:
            self.log.debug("%s: %s", query, values)
            if values:
                params = [query, *self._asyncpg_bind_values(values)]
            else:
                params = [query]
            if returns_rows is None:
                returns_rows = not self._is_write_without_returning(query)
            if not returns_rows:
                res = await connection.execute(*params)
                try:
                    rows_affected = int(res.split(" ")[-1])
                except Exception:  # pragma: nocoverage
                    rows_affected = 0
                return StatementResult(rows_affected, [])

            rows = await connection.fetch(*params)
            return StatementResult(len(rows), rows)

    @PostgresqlClient.translate_exceptions
    async def execute_described(self, query: str, values: list[Any] | None = None) -> DescribedResult:
        async with self.acquire_connection() as connection:
            self.log.debug("%s: %s", query, values)
            bind_values = self._asyncpg_bind_values(values) if values else []
            if self._is_write_without_returning(query):
                status = await connection.execute(query, *bind_values)
                last_token = status.split(" ")[-1]
                return DescribedResult(columns=(), rows=(), row_count=int(last_token) if last_token.isdigit() else 0)
            statement = await connection.prepare(query)
            columns = tuple(attribute.name for attribute in statement.get_attributes())
            rows = await statement.fetch(*bind_values)
            return DescribedResult(columns=columns, rows=tuple(tuple(row) for row in rows), row_count=len(rows))
