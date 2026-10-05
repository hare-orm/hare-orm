from __future__ import annotations

import abc
import asyncio
import errno
import os
import sqlite3
from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from typing import Any, ClassVar, TypeVar, cast

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.declarations import ShellCommand
from hare.dialects.base.connection.connection_option import ConnectionOption
from hare.dialects.sqlite.client.constants import (
    NON_PRAGMA_KWARGS,
    SQLITE_CLOSE_TIMEOUT_SECONDS,
    SQLITE_DEFAULT_CASE_SENSITIVE_LIKE,
    SQLITE_FOREIGN_KEY_FAILED_MESSAGE,
    SQLITE_IN_MEMORY_URI_MARKERS,
    SQLITE_NULL_BYTE_ESCAPE,
    SQLITE_READONLY_RESULT_CODE,
    SQLITE_SHELL_PROGRAM,
    SQLITE_SPATIAL_DEFAULT_LIBRARY,
    SQLITE_SPATIAL_LIBRARY_OPTION,
    SQLITE_SPATIAL_METADATA_OPTION,
    SQLITE_SPATIAL_PROJ_DATABASE_OPTION,
    SQLITE_TOO_MANY_JOINED_TABLES_MESSAGES,
    SQLITE_TOO_MANY_VARIABLES_MESSAGE,
    SQLITE_TRIGGER_RECURSION_LIMIT_MESSAGE,
)
from hare.dialects.sqlite.client.declarations import SqliteDriverErrors
from hare.dialects.sqlite.constants import (
    SQLITE_CONNECTION_OPTIONS,
    SQLITE_DEFAULT_FOREIGN_KEYS,
    SQLITE_DEFAULT_JOURNAL_MODE,
    SQLITE_DEFAULT_JOURNAL_SIZE_LIMIT,
    SQLITE_DIALECT,
    SQLITE_IN_MEMORY_FILENAME,
    SQLITE_NULL_BYTE,
    SQLITE_REGEXP_OPTION,
    SQLITE_SPATIAL_EXTENSION_OPTION,
    SQLITE_TRANSACTION_ABORTED_MESSAGE,
    SQLITE_VECTOR_EXTENSION_OPTION,
)
from hare.dialects.sqlite.enums import SpatialiteMetadata
from hare.dialects.sqlite.exceptions import SqliteTriggerRecursionLimitError
from hare.dialects.sqlite.query.declarations import SqliteQuery
from hare.dialects.sqlite.spatial.sqlite_spatial_extension import SqliteSpatialExtension
from hare.dialects.sqlite.vectors.sqlite_vector_extension import SqliteVectorExtension
from hare.exceptions import (
    ConfigurationError,
    DBConnectionError,
    HareError,
    IntegrityError,
    OperationalError,
    TooManyParametersError,
    TransactionManagementError,
    UnSupportedError,
)
from hare.instrumentation.pools.pool_registry import PoolRegistry

T = TypeVar("T")


CoroutineFunction = Callable[..., Coroutine[None, None, T]]


class SqliteClient(DatabaseClient):
    """What every SQLite driver shares: the connection's settings and PRAGMAs, its one connection
    taken like a pool's, the database file, scripts split into statements and the translation of a
    driver's errors into hare's. A driver (``hare.dialects.sqlite.drivers``) opens the connection,
    runs the statements and names its exception classes (``driver_errors``)."""

    native_python_types = frozenset({bytes, str, int, float})
    query_class = SqliteQuery
    # The transaction client interrupts an over-running query itself.
    enforces_statement_timeout_itself = True
    # The transaction client sets the connection's busy timeout for the transaction.
    enforces_lock_timeout_itself = True
    dialect = SQLITE_DIALECT
    features = SQLITE_DIALECT.features.replace(supports_pool_status=True)

    #: The exception classes of the driver.
    driver_errors: ClassVar[SqliteDriverErrors]
    #: The version of the SQLite library the driver runs, as text.
    library_version: ClassVar[str] = ""
    #: Whether the driver's library builds an automatic index that ignores the collating sequence of
    #: the comparison it serves - its connections then build no automatic indexes.
    has_automatic_index_collation_fault: ClassVar[bool] = False
    #: Whether the driver's library reports a foreign key broken by a statement with ``RETURNING`` as
    #: a plain error - raised as the constraint failure it is.
    has_returning_foreign_key_error_fault: ClassVar[bool] = False

    def __init__(self, file_path: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not isinstance(file_path, str) or not file_path:
            raise ConfigurationError(
                f"SQLite connection needs a non-empty file_path (a file name or ':memory:'), got {file_path!r}"
            )
        self.filename = file_path
        #: Whether the dialect's placeholders name their parameters' numbers (``?1``) - the values of
        #: a statement it built are then bound by number (``get_values_by_number()``).
        self.binds_parameters_by_number: bool = self.dialect.parameters.numbers_parameters
        settings = {key: value for key, value in kwargs.items() if key not in NON_PRAGMA_KWARGS}
        self.install_regexp_functions: bool = SQLITE_REGEXP_OPTION.parse(kwargs.get(SQLITE_REGEXP_OPTION.name, False))
        #: Whether each connection loads the sqlite-vec extension - VectorField's distances.
        self.load_sqlite_vec: bool = SQLITE_VECTOR_EXTENSION_OPTION.parse(
            kwargs.get(SQLITE_VECTOR_EXTENSION_OPTION.name, False)
        )
        if self.load_sqlite_vec:
            SqliteVectorExtension.raise_if_unavailable()
            self.features = self.features.replace(supports_vector_search=True)
        #: The SpatiaLite extension each connection loads - ``hare.gis``'s spatial SQL; None without it.
        self.spatial_extension: SqliteSpatialExtension | None = None
        if SQLITE_SPATIAL_EXTENSION_OPTION.parse(kwargs.get(SQLITE_SPATIAL_EXTENSION_OPTION.name, False)):
            SqliteSpatialExtension.raise_if_unavailable()
            created_metadata = SpatialiteMetadata(
                SQLITE_SPATIAL_METADATA_OPTION.parse(
                    kwargs.get(SQLITE_SPATIAL_METADATA_OPTION.name, SpatialiteMetadata.WGS84)
                )
            )
            self.spatial_extension = SqliteSpatialExtension(
                self._get_text_option(SQLITE_SPATIAL_LIBRARY_OPTION, kwargs) or SQLITE_SPATIAL_DEFAULT_LIBRARY,
                self._get_text_option(SQLITE_SPATIAL_PROJ_DATABASE_OPTION, kwargs),
                created_metadata,
            )
            # The SRIDs the metadata has are read by the first connection, which installs the extension.
            has_metadata = created_metadata is not SpatialiteMetadata.NONE
            self.features = self.features.replace(
                supports_spatial=True, supports_geography=has_metadata, supports_spatial_index=has_metadata
            )
        else:
            spatial_options = (
                SQLITE_SPATIAL_LIBRARY_OPTION,
                SQLITE_SPATIAL_PROJ_DATABASE_OPTION,
                SQLITE_SPATIAL_METADATA_OPTION,
            )
            for option in spatial_options:
                if kwargs.get(option.name) is not None:
                    raise ConfigurationError(
                        f"{option.name} configures SpatiaLite, which only "
                        f"{SQLITE_SPATIAL_EXTENSION_OPTION.name}=True loads"
                    )
        defaults = {
            "journal_mode": SQLITE_DEFAULT_JOURNAL_MODE,
            "journal_size_limit": SQLITE_DEFAULT_JOURNAL_SIZE_LIMIT,
            "foreign_keys": SQLITE_DEFAULT_FOREIGN_KEYS,
            "case_sensitive_like": SQLITE_DEFAULT_CASE_SENSITIVE_LIKE,
        }
        if self.has_automatic_index_collation_fault:
            defaults["automatic_index"] = False
        #: PRAGMA name -> checked value, as sent to the connection.
        self.pragmas: dict[str, str | int] = {
            name: ("ON" if value else "OFF") if isinstance(value, bool) else value
            for name, value in SQLITE_CONNECTION_OPTIONS.read(settings, defaults).items()
        }
        SQLITE_CONNECTION_OPTIONS.raise_for_unknown(settings, "sqlite")
        if self.has_automatic_index_collation_fault and self.pragmas["automatic_index"] == "ON":
            raise ConfigurationError(
                f"automatic_index=ON on SQLite {self.library_version}: its automatic indexes ignore the "
                "collating sequence of a comparison, so a decimal or time comparison over a joined table "
                "finds no rows - fixed in SQLite 3.41.1"
            )

        #: The driver's open connection, None before the first statement and after close().
        self._connection: Any = None
        self._lock = asyncio.Lock()
        #: The task running the top-level transaction that currently holds ``_lock``, if any.
        self._transaction_task: asyncio.Task[Any] | None = None

    @staticmethod
    def _get_text_option(option: ConnectionOption, kwargs: dict[str, Any]) -> str | None:
        """A text setting of the connection.

        Args:
            option: The setting.
            kwargs: The connection's settings.

        Returns:
            The text; None when the setting isn't given.

        Raises:
            ConfigurationError: It isn't a non-empty string.
        """
        raw_value = kwargs.get(option.name)
        if raw_value is None:
            return None
        text = option.parse(raw_value)
        if not text:
            raise ConfigurationError(f"{option.name} must be a non-empty string")
        return cast("str", text)

    @abc.abstractmethod
    async def swap_busy_timeout(self, connection: Any, milliseconds: int) -> int:
        """Sets the connection's busy timeout - how long a statement waits for another connection's
        lock.

        Args:
            connection: The driver's connection.
            milliseconds: The new timeout.

        Returns:
            The timeout before.
        """

    @asynccontextmanager
    async def lock_timeout_session(self, seconds: float) -> AsyncGenerator[DatabaseClient]:
        """This client, its connection's busy timeout set for the block and restored after it."""
        async with self.acquire_connection() as connection:
            previous_milliseconds = await self.swap_busy_timeout(connection, max(1, round(seconds * 1000)))
        try:
            yield self
        finally:
            async with self.acquire_connection() as connection:
                await self.swap_busy_timeout(connection, previous_milliseconds)

    def get_address(self) -> str:
        return self.filename

    def get_pool_occupancy(self) -> tuple[int, int, int, int, int] | None:
        if not self._connection:
            return None
        in_use = 1 if self._lock.locked() else 0
        return 1, 1 - in_use, self.pool_statistics.acquiring, 1, 1

    async def close(self) -> None:
        """Closes the connection once the transaction or query currently using it has finished,
        waiting at most ``SQLITE_CLOSE_TIMEOUT_SECONDS`` before closing it anyway.

        Raises:
            TransactionManagementError: Called from inside a transaction open on this connection.
        """
        if self._transaction_task is not None and self._transaction_task is asyncio.current_task():
            raise TransactionManagementError("Cannot close a connection while inside a transaction")
        try:
            await asyncio.wait_for(self._lock.acquire(), SQLITE_CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            self.log.warning("Closing connection %s while a transaction or query still uses it", self.connection_alias)
            await self._close_unlocked()
            return
        try:
            await self._close_unlocked()
        finally:
            self._lock.release()

    async def _close_unlocked(self) -> None:
        PoolRegistry.remove(self)
        if self._connection:
            await self._connection.close()
            self.log.debug(
                "Closed connection %s with params: filename=%s %s",
                self._connection,
                self.filename,
                " ".join(f"{pragma}={value}" for pragma, value in self.pragmas.items()),
            )

            self._connection = None

    async def db_create(self) -> None:
        """Checks that the database file doesn't exist yet - SQLite itself creates it on the first
        connection, like CREATE DATABASE on Postgres, which refuses an existing database.

        Raises:
            OperationalError: If the database file already exists.
        """
        if self.filename != SQLITE_IN_MEMORY_FILENAME and os.path.exists(self.filename):
            raise OperationalError(f'database file "{self.filename}" already exists')

    async def db_delete(self) -> None:
        await self.close()
        try:
            os.remove(self.filename)
        except FileNotFoundError:  # pragma: nocoverage
            pass
        except OSError as error:
            if error.errno != errno.EINVAL:  # fix: "sqlite+aiosqlite://:memory:" in Windows
                raise error

    @staticmethod
    def _is_read_only_transaction_write(client: Any, error: BaseException) -> bool:
        """Whether ``error`` is SQLite refusing a write because the transaction is read-only
        (``PRAGMA query_only``).

        Args:
            client: The client the statement ran on.
            error: The error.

        Returns:
            True for a write refused in a read-only transaction.
        """
        return (
            client.is_transaction_client
            and client._transaction_options.read_only
            and getattr(error, "sqlite_errorcode", None) == SQLITE_READONLY_RESULT_CODE
        )

    checks_aborted_transactions: ClassVar[bool] = True
    aborted_transaction_message: ClassVar[str] = SQLITE_TRANSACTION_ABORTED_MESSAGE

    @staticmethod
    def translate_driver_error(
        client: Any,
        error: BaseException,
        sql: str | None,
        parameters: Any,
        args: tuple[Any, ...],
        is_query_executing: bool,
    ) -> BaseException:
        # The driver's exception classes are read only once a statement failed.
        driver_errors = client.driver_errors
        if isinstance(error, driver_errors.operational):
            # A narrower, catchable type for exactly this one message - Model.delete()/
            # QuerySet.delete() retry with a Python-side cascade walk on it instead of a native ON
            # DELETE CASCADE that can't finish past SQLITE_LIMIT_TRIGGER_DEPTH.
            if SQLITE_TRIGGER_RECURSION_LIMIT_MESSAGE in str(error):
                return SqliteTriggerRecursionLimitError(error, sql=sql, parameters=parameters)
            if client.has_returning_foreign_key_error_fault and str(error) == SQLITE_FOREIGN_KEY_FAILED_MESSAGE:
                return IntegrityError(error, sql=sql, parameters=parameters)
            if SQLITE_TOO_MANY_VARIABLES_MESSAGE in str(error):
                return TooManyParametersError(error, sql=sql, parameters=parameters)
            if any(message in str(error) for message in SQLITE_TOO_MANY_JOINED_TABLES_MESSAGES):
                return OperationalError(
                    f"{error} - the query joins more tables than SQLite allows in one statement. Every "
                    "relation hop in a filter()/exclude()/order_by()/values()/select_related() path adds a "
                    "JOIN (a many-to-many hop two); shorten the relation paths.",
                    sql=sql,
                    parameters=parameters,
                )
            if SqliteClient._is_read_only_transaction_write(client, error):
                # The same error PostgreSQL gives a write in a read-only transaction.
                return TransactionManagementError(f"cannot write in a read-only transaction: {error}")
            return client._get_operational_error(error, sql=sql, parameters=parameters)
        if isinstance(error, driver_errors.integrity):
            return IntegrityError(error, sql=sql, parameters=parameters)
        # An interface error means the connection or cursor is unusable - a DBConnectionError. The
        # other errors of a statement are OperationalError.
        if isinstance(error, driver_errors.interface):
            return DBConnectionError(error, sql=sql, parameters=parameters)
        if isinstance(error, driver_errors.statement):
            return OperationalError(error, sql=sql, parameters=parameters)
        # Any other database error - a corrupted database file raises one.
        if isinstance(error, driver_errors.database):
            return OperationalError(error, sql=sql, parameters=parameters)
        if isinstance(error, HareError):
            return error
        # A driver raises these outside its own error classes while binding a query parameter - an
        # int beyond int64 (OverflowError), or a value of a type it can't adapt. Left as-is for
        # commit()/rollback(), whose on-commit callbacks are application code.
        if isinstance(error, (OverflowError, ValueError, TypeError)):
            if isinstance(error, ValueError) and str(error) == driver_errors.closed_connection_message:
                return DBConnectionError(error, sql=sql, parameters=parameters)
            if is_query_executing:
                return OperationalError(error, sql=sql, parameters=parameters)
        return error

    async def get_shell_command(self) -> ShellCommand:
        if any(marker in self.filename for marker in SQLITE_IN_MEMORY_URI_MARKERS):
            raise UnSupportedError(
                f"{self.connection_alias!r} is an in-memory SQLite database - it lives in this process only, "
                "no other program can open it"
            )
        return ShellCommand((SQLITE_SHELL_PROGRAM, self.filename))

    @staticmethod
    def get_values_by_number(values: Sequence[Any]) -> dict[str, Any]:
        """The values of a statement with numbered placeholders (``?1``), keyed by their numbers -
        ``sqlite3`` binds a sequence to such placeholders with a deprecation warning before Python
        3.14, a mapping in every version.

        Args:
            values: The values, in the order of their numbers.

        Returns:
            The mapping.
        """
        return {str(number): value for number, value in enumerate(values, 1)}

    @staticmethod
    def _escape_null_bytes(query: str) -> str:
        return query.replace(SQLITE_NULL_BYTE, SQLITE_NULL_BYTE_ESCAPE)

    @staticmethod
    def split_script(script: str) -> list[str]:
        """Split a SQL script into statements, ignoring semicolons in literals, comments and trigger bodies.

        Args:
            script: SQL script text.

        Returns:
            Non-empty statements without surrounding whitespace.
        """
        statements: list[str] = []
        statement_start = 0
        semicolon_position = script.find(";")
        while semicolon_position != -1:
            candidate = script[statement_start : semicolon_position + 1]
            if sqlite3.complete_statement(candidate):
                statements.append(candidate.strip())
                statement_start = semicolon_position + 1
            semicolon_position = script.find(";", semicolon_position + 1)
        remainder = script[statement_start:].strip()
        if remainder:
            statements.append(remainder)
        return [statement.rstrip(";").strip() for statement in statements if statement.rstrip(";").strip()]
