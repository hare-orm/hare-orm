from __future__ import annotations

import abc
import asyncio
import contextvars
import errno
import os
import sqlite3
from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from typing import Any, ClassVar, TypeVar, cast

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.declarations import ShellCommand
from hare.dialects.base.connection.connection_option import ConnectionOption
from hare.dialects.sqlite.client.constants import (
    NON_PRAGMA_KWARGS,
    SQLITE_ATTACHED_DATABASES_OPTION_NAME,
    SQLITE_CLOSE_TIMEOUT_SECONDS,
    SQLITE_DEFAULT_CASE_SENSITIVE_LIKE,
    SQLITE_FUNCTION_ERROR_MESSAGES,
    SQLITE_LIKE_PATTERN_TOO_LONG_ERROR,
    SQLITE_LIKE_PATTERN_TOO_LONG_MESSAGE,
    SQLITE_MISSING_CONFLICT_TARGET_MESSAGE,
    SQLITE_READONLY_RESULT_CODE,
    SQLITE_RESERVED_DATABASE_NAMES,
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
from hare.dialects.sqlite.client.sqlite_migration_lock import SqliteMigrationLock
from hare.dialects.sqlite.constants import (
    SQLITE_CONNECTION_OPTIONS,
    SQLITE_DEFAULT_FOREIGN_KEYS,
    SQLITE_DEFAULT_JOURNAL_MODE,
    SQLITE_DEFAULT_JOURNAL_SIZE_LIMIT,
    SQLITE_DIALECT,
    SQLITE_FILE_FORMAT_PRAGMAS,
    SQLITE_IN_MEMORY_FILENAME,
    SQLITE_REGEXP_OPTION,
    SQLITE_SPATIAL_EXTENSION_OPTION,
    SQLITE_TRANSACTION_ABORTED_MESSAGE,
    SQLITE_VECTOR_EXTENSION_OPTION,
)
from hare.dialects.sqlite.enums import SpatialiteMetadata
from hare.dialects.sqlite.exceptions import SqliteTriggerRecursionLimitError
from hare.dialects.sqlite.functions.sqlite_function_errors import SqliteFunctionErrors
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
    ValidationError,
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

    #: The connections whose foreign keys a migration switched off in the current task - a delete in
    #: its ``RunPython`` cascades in Python there.
    foreign_keys_switched_off: ClassVar[contextvars.ContextVar[frozenset[str]]] = contextvars.ContextVar(
        "hare_sqlite_foreign_keys_switched_off", default=frozenset()
    )

    @staticmethod
    def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """Preserves each result column's exact name and positional value.

        ``sqlite3.Row`` looks up names without regard to ASCII case; ``dict(row)`` therefore
        reads the first value twice when the result contains both ``a`` and ``A``.

        Args:
            row: A named row returned by the driver.

        Returns:
            The result names paired with the values at their positions.
        """
        return dict(zip(row.keys(), row, strict=True))

    async def execute_dicts(self, query: str, values: list[Any] | None = None) -> list[dict[str, Any]]:
        """Reads dictionaries from positional rows, preserving case-distinct output names.

        The cursor supplies the names once for the batch. Reading plain tuples avoids building
        a named row and fetching its keys again for every returned row.

        Args:
            query: The SQL statement.
            values: Its bound parameters.

        Returns:
            The rows keyed by their exact output names.
        """
        result = await self.execute(query, values, returns_rows=True, rows_by_position=True)
        columns = result.column_names
        return [dict(zip(columns, row, strict=True)) for row in result.rows]

    def enforces_foreign_keys(self) -> bool:
        return super().enforces_foreign_keys() and (
            self.connection_alias not in SqliteClient.foreign_keys_switched_off.get()
        )

    def __init__(self, file_path: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not isinstance(file_path, str) or not file_path:
            raise ConfigurationError(
                f"SQLite connection needs a non-empty file_path (a file name or ':memory:'), got {file_path!r}"
            )
        self.filename = file_path
        #: The message of the last error one of hare's functions raised on the connection.
        self.function_errors = SqliteFunctionErrors()
        #: Whether the dialect's placeholders name their parameters' numbers (``?1``) - the values of
        #: a statement it built are then bound by number (``get_values_by_number()``).
        self.binds_parameters_by_number: bool = self.dialect.parameters.numbers_parameters
        settings = {key: value for key, value in kwargs.items() if key not in NON_PRAGMA_KWARGS}
        #: Attached database name -> its file, attached by each connection - the schemas of its models.
        self.attached_databases: dict[str, str] = self._get_attached_databases(
            kwargs.get(SQLITE_ATTACHED_DATABASES_OPTION_NAME)
        )
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
            # Checked once its settings are: a setting's own error stands on any Python.
            SqliteSpatialExtension.raise_if_unavailable()
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
        pragmas = {
            name: ("ON" if value else "OFF") if isinstance(value, bool) else value
            for name, value in SQLITE_CONNECTION_OPTIONS.read(settings, defaults).items()
        }
        #: PRAGMA name -> checked value, as sent to the connection - the file format's first, in their order.
        self.pragmas: dict[str, str | int] = {
            **{name: pragmas.pop(name) for name in SQLITE_FILE_FORMAT_PRAGMAS if name in pragmas},
            **pragmas,
        }
        SQLITE_CONNECTION_OPTIONS.raise_for_unknown(settings, "sqlite")

        #: The driver's open connection, None before the first statement and after close().
        self._connection: Any = None
        self._lock = asyncio.Lock()
        #: The task running the top-level transaction that currently holds ``_lock``, if any.
        self._transaction_task: asyncio.Task[Any] | None = None

    @staticmethod
    def _get_attached_databases(raw_value: Any) -> dict[str, str]:
        """The databases each connection attaches.

        Args:
            raw_value: The setting - ``{name: file}``, or the text ``name:file,name:file`` of a database URL;
                None for none.

        Returns:
            Name -> file.

        Raises:
            ConfigurationError: The setting isn't such names and files, or takes a name SQLite keeps.
        """
        if raw_value is None:
            return {}
        if isinstance(raw_value, str):
            pairs = [pair.partition(":") for pair in raw_value.split(",") if pair]
            if not pairs or any(not separator for __, separator, __ in pairs):
                raise ConfigurationError(
                    f"{SQLITE_ATTACHED_DATABASES_OPTION_NAME} takes name:file pairs separated by commas, "
                    f"got {raw_value!r}"
                )
            names = [name for name, __, __ in pairs]
            if len(set(names)) != len(names):
                raise ConfigurationError(
                    f"{SQLITE_ATTACHED_DATABASES_OPTION_NAME}: a database name is given twice in {raw_value!r}"
                )
            raw_value = {name: file for name, __, file in pairs}
        if not isinstance(raw_value, dict):
            raise ConfigurationError(
                f"{SQLITE_ATTACHED_DATABASES_OPTION_NAME} takes a dict of database names to files, got {raw_value!r}"
            )
        for name, file in raw_value.items():
            if not isinstance(name, str) or not name or "\x00" in name:
                raise ConfigurationError(
                    f"{SQLITE_ATTACHED_DATABASES_OPTION_NAME}: a database name must be a non-empty text, got {name!r}"
                )
            if name.lower() in SQLITE_RESERVED_DATABASE_NAMES:
                raise ConfigurationError(
                    f"{SQLITE_ATTACHED_DATABASES_OPTION_NAME}: SQLite keeps the database name {name!r} itself"
                )
            if not isinstance(file, str) or not file or "\x00" in file:
                raise ConfigurationError(
                    f"{SQLITE_ATTACHED_DATABASES_OPTION_NAME}: the file of database {name!r} must be a non-empty "
                    f"text, got {file!r}"
                )
        return dict(raw_value)

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

    def holds_database_in_connection(self) -> bool:
        # An in-memory database is the connection's own.
        return self.filename == SQLITE_IN_MEMORY_FILENAME

    def hold_migration_lock(self) -> AbstractAsyncContextManager[None]:
        # An in-memory database lives in this process only - no other one migrates it.
        if self.filename == SQLITE_IN_MEMORY_FILENAME:
            return nullcontext()
        return SqliteMigrationLock.hold(self.filename)

    def get_pool_bounds(self) -> tuple[int, int]:
        # The client's one connection.
        return 1, 1

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
        connection, and ``db_create()`` refuses an existing database.

        Raises:
            OperationalError: If the database file already exists.
        """
        if self.filename != SQLITE_IN_MEMORY_FILENAME and os.path.exists(self.filename):
            raise OperationalError(f"database file {self.filename!r} already exists")

    async def generate_schema(self, safe: bool) -> None:
        """Creates every model of this connection in the database, in one transaction - outside one,
        each statement of the script commits on its own, a write of the database file each.

        Args:
            safe: Whether each object is created only when it doesn't exist yet.
        """
        # Connected first, so the DDL is written for the features of the library's own version.
        async with self.acquire_connection():
            pass
        schema = self.get_schema_sql(safe)
        self.log.debug("Creating schema: %s", schema)
        if schema:
            async with self._in_transaction() as connection:
                await connection.execute_script(schema)

    async def db_delete(self) -> None:
        await self.close()
        try:
            os.remove(self.filename)
        except FileNotFoundError:
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
            if str(error) in SQLITE_FUNCTION_ERROR_MESSAGES:
                # sqlite3 drops the message of an error one of hare's functions raised - kept aside.
                function_message = client.function_errors.take_message()
                if function_message is not None:
                    return OperationalError(function_message, sql=sql, parameters=parameters)
            # A narrower, catchable type for exactly this one message - Model.delete()/
            # QuerySet.delete() retry with a Python-side cascade walk on it instead of a native ON
            # DELETE CASCADE that can't finish past SQLITE_LIMIT_TRIGGER_DEPTH.
            if SQLITE_TRIGGER_RECURSION_LIMIT_MESSAGE in str(error):
                return SqliteTriggerRecursionLimitError(error, sql=sql, parameters=parameters)
            if SQLITE_TOO_MANY_VARIABLES_MESSAGE in str(error):
                return TooManyParametersError(error, sql=sql, parameters=parameters)
            if SQLITE_LIKE_PATTERN_TOO_LONG_MESSAGE in str(error):
                return ValidationError(SQLITE_LIKE_PATTERN_TOO_LONG_ERROR.format(error=error))
            if any(message in str(error) for message in SQLITE_TOO_MANY_JOINED_TABLES_MESSAGES):
                return OperationalError(
                    f"{error} - the query joins more tables than SQLite allows in one statement. Every "
                    "relation hop in a filter()/exclude()/order_by()/values()/select_related() path adds a "
                    "JOIN (a many-to-many hop two); shorten the relation paths.",
                    sql=sql,
                    parameters=parameters,
                )
            if SqliteClient._is_read_only_transaction_write(client, error):
                # hare's error of a write in a read-only transaction.
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
        if self.filename == SQLITE_IN_MEMORY_FILENAME:
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
    def is_missing_conflict_target_error(error: BaseException) -> bool:
        """Whether an INSERT failed because its ``ON CONFLICT`` names columns no primary key or
        unique index of the table has.

        Args:
            error: The exception, hare's or the driver's.

        Returns:
            True for that refusal.
        """
        return SQLITE_MISSING_CONFLICT_TARGET_MESSAGE in str(error)

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
