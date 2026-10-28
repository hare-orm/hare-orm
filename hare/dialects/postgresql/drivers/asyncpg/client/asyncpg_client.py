from __future__ import annotations

import asyncio
import contextlib
import datetime
import decimal
import functools
import gc
import logging
import time
import traceback
import urllib.parse
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

import asyncpg

from hare.dialects.base.client.connect_failure_reports import ConnectFailureReports
from hare.dialects.base.results.described_result import DescribedResult
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.postgresql.client.pool_timeouts import PoolTimeouts
from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient
from hare.dialects.postgresql.client.postgresql_connect_errors import PostgresqlConnectErrors
from hare.dialects.postgresql.client.transaction_pooler import TransactionPooler
from hare.dialects.postgresql.constants import POSTGRES_SERVER_VERSION_NUMBER_SQL
from hare.dialects.postgresql.drivers.asyncpg.asyncpg_date_codec import AsyncpgDateCodec
from hare.dialects.postgresql.drivers.asyncpg.asyncpg_extension_type_codecs import AsyncpgExtensionTypeCodecs
from hare.dialects.postgresql.drivers.asyncpg.asyncpg_interval_codec import AsyncpgIntervalCodec
from hare.dialects.postgresql.drivers.asyncpg.asyncpg_timestamp_codec import AsyncpgTimestampCodec
from hare.dialects.postgresql.drivers.asyncpg.asyncpg_timestamptz_codec import AsyncpgTimestamptzCodec
from hare.dialects.postgresql.drivers.asyncpg.client.constants import ASYNCPG_CONNECTION_PARAMETERS
from hare.dialects.postgresql.drivers.asyncpg.constants import (
    ASYNCPG_ARGUMENT_COUNT_MESSAGE_PREFIX,
    ASYNCPG_CONNECTION_OPTIONS,
    ASYNCPG_DECODE_FRAME_NAME_PREFIX,
    ASYNCPG_POOL_ONLY_PARAMETERS,
    ASYNCPG_ROOT_CERTIFICATE_DSN_PARAMETER,
    ASYNCPG_ROOT_CERTIFICATE_DSN_PREFIX,
    ASYNCPG_ROOT_CERTIFICATE_SETTING,
    ASYNCPG_SIMPLE_QUERY_RESULT_FRAME_NAME,
    ASYNCPG_TOO_MANY_ARGUMENTS_MESSAGE,
    EPOCH_YEAR,
    FAST_SCALAR_BIND_TYPES,
    PLAIN_BIND_TYPE_TUPLE,
    PLAIN_BIND_TYPES,
    POOL_CLOSE_TIMEOUT_SECONDS,
)
from hare.dialects.postgresql.drivers.asyncpg.pre_epoch_local_zone import PreEpochLocalZone
from hare.dialects.postgresql.drivers.constants import (
    POSTGRES_SHELL_SSL_MODE_BY_SWITCH,
    POSTGRES_SHELL_SSL_MODE_VARIABLE,
    POSTGRES_SHELL_SSL_ROOT_CERT_VARIABLE,
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
from hare.instrumentation.pools.pool_metrics import PoolMetrics
from hare.instrumentation.queries.query_tags import QueryTags
from hare.native.native_modules import NativeModules
from hare.time import Timezone

if TYPE_CHECKING:
    from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_transaction_client import AsyncpgTransactionClient


class AsyncpgClient(PostgresqlClient):
    """The PostgreSQL client of the asyncpg driver."""

    driver_name = "postgresql+asyncpg"
    connections_bound_to_event_loop = True
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
        unknown_parameters = sorted(
            set(self.extra) - ASYNCPG_CONNECTION_PARAMETERS - {ASYNCPG_ROOT_CERTIFICATE_SETTING}
        )
        if unknown_parameters:
            raise ConfigurationError(
                f"Unknown connection parameter(s) {unknown_parameters!r} for the asyncpg Postgres driver"
            )
        self.extra.update(ASYNCPG_CONNECTION_OPTIONS.read(self.extra))
        #: The file of the certificates the server's is checked against, None for libpq's own.
        self.ssl_root_cert: str | None = self.extra.pop(ASYNCPG_ROOT_CERTIFICATE_SETTING, None)
        #: The DSN asyncpg reads the root certificate from - it takes no keyword for it.
        self.root_certificate_dsn = (
            None
            if self.ssl_root_cert is None
            else ASYNCPG_ROOT_CERTIFICATE_DSN_PREFIX
            + urllib.parse.urlencode({ASYNCPG_ROOT_CERTIFICATE_DSN_PARAMETER: self.ssl_root_cert})
        )
        if self.ssl_root_cert is not None and not isinstance(self.extra.get("ssl"), (str, bool, type(None))):
            raise ConfigurationError(
                f"{ASYNCPG_ROOT_CERTIFICATE_SETTING} takes a TLS mode - an SSLContext holds its certificates itself"
            )
        #: The connects whose caller was cancelled - finished on their own, their connections closed.
        self.connects_left_running: set[asyncio.Task[asyncpg.Connection]] = set()
        #: Each open pool's event, by the pool's id(), closing a connection that finishes opening after
        #: the pool is gone - an asyncpg pool takes no weak reference.
        self.pool_closed_events: dict[int, asyncio.Event] = {}

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
        which fails before 1970 on Windows - such a value gets the system zone's offset attached here,
        as a ``PreEpochLocalZone`` the ``timestamp`` codec drops again.

        Args:
            value: A naive datetime before 1970.

        Returns:
            `value` itself when ``astimezone()`` can convert it, the value in the local offset otherwise.
        """
        try:
            value.astimezone()
        except (OSError, OverflowError, ValueError):
            return value.replace(tzinfo=PreEpochLocalZone(Timezone.make_system_local_aware(value).utcoffset()))
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
        self.raise_if_root_certificate_unusable(self.ssl_root_cert)
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
            closes_before = self.closes_started
            pool = await self._create_pool_with_retry(password=self._get_password(), **self._template)
            if self.closes_started != closes_before:
                # Closed while it opened - it belongs to no client; the next use opens a pool anew.
                await self._close_pool(pool)
                return
            self._pool = pool
            await self._post_connect()
            self.log.debug("Created connection pool %s with params: %s", self._pool, self._template)
        except Exception as error:
            connect_error = PostgresqlConnectErrors.get_error(self, error, with_db)
            if connect_error is None:
                raise
            raise connect_error from None

    def get_shell_ssl_environment(self) -> dict[str, str]:
        # A mode from a DB_URL (``sslmode=``, ``ssl=true``); an SSLContext has no libpq form.
        ssl = self.extra.get("ssl")
        environment = {}
        if isinstance(ssl, bool):
            environment[POSTGRES_SHELL_SSL_MODE_VARIABLE] = POSTGRES_SHELL_SSL_MODE_BY_SWITCH[ssl]
        elif isinstance(ssl, str):
            environment[POSTGRES_SHELL_SSL_MODE_VARIABLE] = ssl
        if self.ssl_root_cert is not None:
            environment[POSTGRES_SHELL_SSL_ROOT_CERT_VARIABLE] = self.ssl_root_cert
        return environment

    def _get_password(self) -> Any:
        """The password asyncpg opens a connection with - the provider's ``get()`` for a connection
        with ``password_provider``: asyncpg asks it for each connection it opens.
        """
        if self.password_provider is not None:
            return self.password_provider.get
        return self.password

    @staticmethod
    async def _skip_default_reset(connection: asyncpg.Connection) -> None:
        """Replaces asyncpg's reset on release (``pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *;
        RESET ALL``) with nothing: hare takes no advisory lock, holdable cursor, LISTEN or
        session-level SET on a pool connection, and the reset costs a round trip per release.
        asyncpg still rolls back an open transaction before this runs.
        """

    async def create_pool(self, **kwargs: Any) -> asyncpg.Pool:
        configured_init = kwargs.pop("init", None)
        configured_connect = kwargs.pop("connect", None) or asyncpg.connect
        pool_failed = asyncio.Event()
        pool = asyncpg.create_pool(
            self.root_certificate_dsn,
            reset=self._skip_default_reset,
            init=functools.partial(self._init_connection, configured_init, pool_failed),
            connect=functools.partial(self._connect_counted, configured_connect, pool_failed),
            **kwargs,
        )
        try:
            opened_pool = await pool
        except BaseException:
            # asyncpg leaves the connections a pool failing to open has opened - and the ones still
            # opening, which it doesn't wait for - open until the process ends.
            pool_failed.set()
            pool.terminate()
            raise
        # A pool closed while it still grows to min_size leaves the connections still opening open
        # too - set on close, the event closes them as they come.
        self.pool_closed_events[id(opened_pool)] = pool_failed
        return opened_pool

    async def _connect_counted(
        self,
        connect: Callable[..., Awaitable[asyncpg.Connection]],
        pool_failed: asyncio.Event,
        *args: Any,
        **kwargs: Any,
    ) -> asyncpg.Connection:
        """Opens a pool connection and counts it with the time it took (``PoolStatistics``).

        Args:
            connect: The configured ``connect`` connection parameter, ``asyncpg.connect`` by default.
            pool_failed: Set once the pool failed to open or was closed - a connection opened after it
                is closed.
            *args: What the pool connects with.
            **kwargs: What the pool connects with.

        Returns:
            The connection.

        Raises:
            DBConnectionError: The pool failed to open while the connection was opening.
        """
        start_time = time.perf_counter()
        connect_task = asyncio.ensure_future(connect(*args, **kwargs))
        try:
            await asyncio.wait([connect_task])
        except asyncio.CancelledError:
            # asyncpg cancelled in the middle of its TLS negotiation leaves a future of its own
            # failing with no one reading it - the connect runs to its end, bounded by its timeout,
            # and its connection is closed.
            self.connects_left_running.add(connect_task)
            connect_task.add_done_callback(self.close_connection_left)
            raise
        connection = connect_task.result()
        if pool_failed.is_set():
            connection.terminate()
            raise DBConnectionError(
                "The connection pool failed to open or was closed while this connection was opening"
            )
        self.pool_statistics.add_connect(time.perf_counter() - start_time)
        return connection

    def close_connection_left(self, connect_task: asyncio.Task[asyncpg.Connection]) -> None:
        """Closes the connection of a connect whose caller was cancelled.

        Args:
            connect_task: The finished connect.
        """
        self.connects_left_running.discard(connect_task)
        if connect_task.cancelled() or connect_task.exception() is not None:
            return
        connect_task.result().terminate()

    @staticmethod
    async def _init_connection(
        configured_init: Callable[[asyncpg.Connection], Any] | None,
        pool_failed: asyncio.Event,
        connection: asyncpg.Connection,
    ) -> None:
        """Prepares a new pool connection: hare's codecs, then the configured ``init`` callback.

        Args:
            configured_init: The ``init`` connection parameter, if any.
            pool_failed: Set once the pool failed to open or was closed.
            connection: The new connection.

        Raises:
            DBConnectionError: The pool failed to open or was closed while the connection was prepared -
                asyncpg puts a connection in the pool only once it is prepared, so the pool's closing
                missed it; raised, it is closed.
        """
        await AsyncpgTimestampCodec.install(connection)
        await AsyncpgTimestamptzCodec.install(connection)
        await AsyncpgDateCodec.install(connection)
        await AsyncpgIntervalCodec.install(connection)
        await AsyncpgExtensionTypeCodecs.install(connection)
        if configured_init is not None:
            await configured_init(connection)
        if pool_failed.is_set():
            raise DBConnectionError(
                "The connection pool failed to open or was closed while this connection was prepared"
            )

    async def listen(
        self, channel: str, callback: Callable[[asyncpg.Connection, int, str, str], None]
    ) -> asyncpg.Connection:
        """Opens a dedicated connection, outside the pool, and LISTENs on ``channel``, calling
        ``callback`` for every NOTIFY. The caller owns the returned connection and closes it. For a
        subscription that reconnects use ``hare.contrib.notify.NotificationListener``.
        """
        self.raise_if_channel_unusable(channel)
        if self.transaction_pooling:
            # A LISTEN holds its session - on the server itself, past the transaction pooler.
            return await TransactionPooler.get_direct_client(self).listen(channel, callback)
        if self.schema:
            # listen() may run before the pool exists - the schema's search_path is set here too.
            self.server_settings["search_path"] = self.schema
        self.raise_if_root_certificate_unusable(self.ssl_root_cert)
        connect_parameters = {
            key: value for key, value in self.extra.items() if key not in ASYNCPG_POOL_ONLY_PARAMETERS
        }
        try:
            connection = await asyncpg.connect(
                self.root_certificate_dsn,
                host=self.host,
                port=self.port,
                user=self.user,
                password=self._get_password(),
                database=self.database,
                connection_class=self.connection_class,
                loop=self.loop,
                server_settings=self.server_settings,
                **connect_parameters,
            )
        except Exception as error:
            connect_error = PostgresqlConnectErrors.get_error(self, error, with_db=True)
            if connect_error is None:
                raise
            raise connect_error from None
        try:
            await connection.add_listener(channel, callback)
        except BaseException:
            # The caller never gets the connection to close.
            connection.terminate()
            raise
        return connection

    async def _pool_acquire(self) -> asyncpg.pool.PoolConnectionProxy:
        """Takes a connection from the pool, waiting at most ``pool_acquire_timeout`` - indefinitely
        when it isn't set.
        """
        pool = self._pool
        assert pool is not None, "_pool_acquire called without create_connection() first"  # nosec B101
        statistics = self.pool_statistics
        statistics.acquiring += 1
        # The wait is measured only while the pool metrics are enabled - else no call is added.
        start_time = time.perf_counter() if PoolMetrics.enabled else 0.0
        try:
            connection = await pool.acquire(timeout=self.pool_acquire_timeout)
        except TimeoutError:
            raise PoolTimeouts.get_error(self, self.pool_acquire_timeout or 0.0) from None
        except self.RETRYABLE_CONNECT_EXCEPTIONS as error:
            # The pool grew and opening its new connection failed - unless the pool was closed meanwhile,
            # which ends the connections it opens: the client was listed again as a pool failing to open.
            if self._pool is pool:
                ConnectFailureReports.record(self, error)
            raise
        finally:
            statistics.acquiring -= 1
        statistics.acquire_count += 1
        if start_time:
            statistics.add_wait(time.perf_counter() - start_time)
        return connection

    def get_pool_occupancy(self) -> tuple[int, int, int, int, int] | None:
        pool = self._pool
        if pool is None:
            return None
        return (
            pool.get_size(),
            pool.get_idle_size(),
            # asyncpg counts no waiters - the tasks taking a connection beyond the idle ones wait.
            max(0, self.pool_statistics.acquiring - pool.get_idle_size()),
            pool.get_min_size(),
            pool.get_max_size(),
        )

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
            # The release waits for the cancel request of a timed-out command to be answered - over a
            # network that stopped answering, for command_timeout at most: asyncpg then terminates it.
            await pool.release(connection, timeout=self.command_timeout)
        except asyncpg.InterfaceError:
            # A terminated pool, or one the connection isn't of.
            closed = True
        if closed:
            with contextlib.suppress(asyncpg.InterfaceError):
                connection.terminate()

    async def _fetch_server_version_number(self) -> int:
        async with self.acquire_connection() as connection:
            return await connection.fetchval(POSTGRES_SERVER_VERSION_NUMBER_SQL)

    def abandon_connections(self) -> None:
        """Drops the pool of a closed event loop - asyncpg's own terminate() writes through that loop.
        A transport closes its socket once collected, and the pool's connections hold one another, so
        they are collected right away: the server ends the sessions now, not at some later collection."""
        if self._pool is None:
            return
        self._pool = None
        gc.collect()

    async def _close(self) -> None:
        if self._pool:
            # Detached before it is closed: a query asking for a connection meanwhile opens a new
            # pool, while the ones checked out of this one finish on it.
            pool, self._pool = self._pool, None
            await self._close_pool(pool)
            self.log.debug("Closed connection pool %s with params: %s", pool, self._template)

    async def _close_pool(self, pool: asyncpg.Pool) -> None:
        """Closes a pool, waiting up to ``POOL_CLOSE_TIMEOUT_SECONDS`` for its checked-out connections -
        and the connections it still opens, as they come."""
        pool_closed = self.pool_closed_events.pop(id(pool), None)
        if pool_closed is not None:
            pool_closed.set()
        try:
            await asyncio.wait_for(pool.close(), POOL_CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            pool.terminate()

    @staticmethod
    def is_missing_database_error(error: BaseException) -> bool:
        return isinstance(error, asyncpg.InvalidCatalogNameError)

    @staticmethod
    def is_invalid_parameter_error(error: BaseException) -> bool:
        # asyncpg refuses a connection keyword argument it doesn't take with a TypeError.
        return isinstance(error, TypeError)

    def get_driver_error(self, error: Exception, call_arguments: tuple[Any, ...]) -> Exception | None:
        if isinstance(error, (ValueError, OverflowError)) and self.is_value_decode_error(error):
            return OperationalError(f"failed to decode a value of the result: {error}")
        if isinstance(error, (asyncpg.SyntaxOrAccessError, asyncpg.exceptions.DataError)):
            return OperationalError(error)
        if isinstance(error, asyncpg.IntegrityConstraintViolationError):
            return IntegrityError(error)
        if isinstance(error, asyncpg.InvalidTransactionStateError):
            return TransactionManagementError(error)
        if isinstance(error, asyncpg.InterfaceError):
            # A client-side refusal to send a statement with too many arguments, or with another
            # number than it takes, not a lost connection - every other InterfaceError is one.
            if str(error).startswith(ASYNCPG_TOO_MANY_ARGUMENTS_MESSAGE):
                return TooManyParametersError(error)
            if str(error).startswith(ASYNCPG_ARGUMENT_COUNT_MESSAGE_PREFIX):
                return OperationalError(error)
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

    @staticmethod
    def is_empty_query_error(error: BaseException) -> bool:
        """Whether asyncpg failed reading the status of a script of no statement - the server's
        empty query response has none, and the connection stays usable.

        Args:
            error: The error.

        Returns:
            True when asyncpg's reading of a simple query's result raised it.
        """
        frames = traceback.extract_tb(error.__traceback__)
        return any(frame.name.endswith(ASYNCPG_SIMPLE_QUERY_RESULT_FRAME_NAME) for frame in frames)

    @staticmethod
    def is_value_decode_error(error: BaseException) -> bool:
        """Whether asyncpg raised an error decoding a result value - not hare's own ValueError.

        Args:
            error: The error.

        Returns:
            True when a frame of the error is asyncpg's decoding of a value.
        """
        frames = traceback.extract_tb(error.__traceback__)
        return any(frame.name.startswith(ASYNCPG_DECODE_FRAME_NAME_PREFIX) for frame in frames)

    async def db_delete(self) -> None:
        try:
            await super().db_delete()
            return
        except asyncpg.InvalidCatalogNameError:
            pass
        except OperationalError as error:
            # A missing database arrives wrapped in OperationalError - the original exception is its
            # __context__.
            if not isinstance(error.__context__, asyncpg.InvalidCatalogNameError):
                raise
        await self.close()

    def get_connection_pool(self) -> asyncpg.Pool | None:
        """The pool this client's connections come from - None before it is created."""
        return self._pool

    def _get_transaction_client(self) -> AsyncpgTransactionClient:
        # Imported here: the modules import each other.
        from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_transaction_client import AsyncpgTransactionClient

        return AsyncpgTransactionClient(self)

    @PostgresqlClient.translate_exceptions
    async def execute_script(self, query: str) -> None:
        async with self.acquire_connection() as connection:
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug(query)
            try:
                await connection.execute(query)
            except AttributeError as error:
                if not AsyncpgClient.is_empty_query_error(error):
                    raise
                # A script of no statement - comments only: the server ran nothing.
                return
            if AsyncpgExtensionTypeCodecs.changes_extensions(query):
                # The codecs of the extension's types - on this connection now, on the pool's other
                # connections once they are opened again.
                await AsyncpgExtensionTypeCodecs.install(connection)
                pool = self.get_connection_pool()
                if pool is not None:
                    await pool.expire_connections()

    @PostgresqlClient.translate_exceptions
    async def execute_many(self, query: str, values: list[Any]) -> None:
        async with self.acquire_connection() as connection:
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug("%s: %s", query, values)
            transaction = connection.transaction()
            await transaction.start()
            try:
                await AsyncpgClient.run_ending_connection_on_cancel(
                    connection, connection.executemany(query, [self._asyncpg_bind_values(row) for row in values])
                )
            except Exception:
                await transaction.rollback()
                raise
            else:
                await transaction.commit()

    @staticmethod
    async def run_ending_connection_on_cancel(connection: Any, call: Awaitable[Any]) -> Any:
        """Awaits a call of a connection that asyncpg can't cancel - a cancelled ``executemany()`` waits
        forever for the server, which waits for the rest of its rows, and stays waiting to write them
        once the connection is gone: the connection is terminated instead, the server rolling its
        transaction back, and the call is left.

        Args:
            connection: The connection.
            call: The call.

        Returns:
            The call's result.
        """
        call_task = asyncio.ensure_future(call)
        # The connection's own error, which a cancellation replaces, is never raised.
        call_task.add_done_callback(lambda task: task.cancelled() or task.exception())
        try:
            # Waited for, not awaited - a cancellation of the caller leaves the call running.
            await asyncio.wait((call_task,))
        except asyncio.CancelledError:
            if not call_task.done():
                connection.terminate()
                call_task.cancel()
            raise
        return call_task.result()

    @PostgresqlClient.translate_exceptions
    async def copy(
        self,
        table: str,
        columns: list[str],
        records: list[tuple[Any, ...]],
        column_types: list[str],
        schema: str | None = None,
    ) -> None:
        """Loads ``records`` into ``table`` through the COPY protocol (``copy_records_to_table``) - no
        ON CONFLICT, no RETURNING. Runs on the connection ``acquire_connection()`` gives, so inside
        a transaction it is part of it. ``column_types`` is unused: asyncpg reads the column types
        itself. A table without ``schema`` is found through the connection's search path. The query tags end the
        statement as a comment after a condition every row meets - asyncpg writes the statement.
        """
        tags_comment = QueryTags.append("")
        async with self.acquire_connection() as connection:
            self.log.debug("COPY %s(%s): %d record(s)", table, columns, len(records))
            await connection.copy_records_to_table(
                table,
                schema_name=schema,
                records=[self._asyncpg_bind_values(row) for row in records],
                columns=columns,
                where=f"TRUE{tags_comment}" if tags_comment else None,
            )

    @PostgresqlClient.translate_exceptions
    async def execute(
        self,
        query: str,
        values: list[Any] | None = None,
        *,
        returns_rows: bool | None = None,
        rows_by_position: bool = False,
    ) -> StatementResult:
        async with self.acquire_connection() as connection:
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug("%s: %s", query, values)
            parameters = [query, *self._asyncpg_bind_values(values)] if values else [query]
            if returns_rows is None:
                returns_rows = not self._is_write_without_returning(query)
            if not returns_rows:
                status = await connection.execute(*parameters)
                try:
                    rows_affected = int(status.split(" ")[-1])
                except ValueError:
                    rows_affected = 0
                return StatementResult(rows_affected, [])

            rows = await connection.fetch(*parameters)
            return StatementResult(len(rows), rows)

    @PostgresqlClient.translate_exceptions
    async def execute_described(self, query: str, values: list[Any] | None = None) -> DescribedResult:
        async with self.acquire_connection() as connection:
            if self.log.isEnabledFor(logging.DEBUG):
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
