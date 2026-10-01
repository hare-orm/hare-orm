import asyncio
import errno
import os
import sqlite3
import time
from collections.abc import Callable, Coroutine
from contextlib import closing
from functools import partial, wraps
from typing import TYPE_CHECKING, Any, TypeVar, cast

import aiosqlite

from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.constants import OBSERVED_METHOD_NAMES, QUERY_EXECUTING_METHOD_NAMES
from hare.dialects.base.features import Features
from hare.dialects.base.results.described_result import DescribedResult
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.sqlite.adapters import SqliteParameterAdapters
from hare.dialects.sqlite.client.sqlite_connection_wrapper import SqliteConnectionWrapper
from hare.dialects.sqlite.constants import (
    AIOSQLITE_NO_ACTIVE_CONNECTION_MESSAGE,
    NON_PRAGMA_KWARGS,
    SQLITE_CLOSE_TIMEOUT_SECONDS,
    SQLITE_CONNECTION_OPTIONS,
    SQLITE_DEFAULT_CASE_SENSITIVE_LIKE,
    SQLITE_DEFAULT_FOREIGN_KEYS,
    SQLITE_DEFAULT_JOURNAL_MODE,
    SQLITE_DEFAULT_JOURNAL_SIZE_LIMIT,
    SQLITE_DIALECT,
    SQLITE_ENABLE_QUERY_ONLY_SQL,
    SQLITE_IN_MEMORY_FILENAME,
    SQLITE_LAST_STATEMENT_CHANGES_SQL,
    SQLITE_NULL_BYTE,
    SQLITE_NULL_BYTE_ESCAPE,
    SQLITE_REGEXP_OPTION,
    SQLITE_TOO_MANY_JOINED_TABLES_MESSAGES,
    SQLITE_TOO_MANY_VARIABLES_MESSAGE,
    SQLITE_TRANSACTION_ABORTED_MESSAGE,
    SQLITE_TRIGGER_RECURSION_LIMIT_MESSAGE,
)
from hare.dialects.sqlite.exceptions import SqliteTriggerRecursionLimitError
from hare.dialects.sqlite.functions.case_mapping import SqliteCaseMapping
from hare.dialects.sqlite.functions.collations.sqlite_decimal_collation import SqliteDecimalCollation
from hare.dialects.sqlite.functions.collations.sqlite_number_text import SqliteNumberText
from hare.dialects.sqlite.functions.comparison.sqlite_cast import SqliteCast
from hare.dialects.sqlite.functions.comparison.sqlite_date_timestamp import SqliteDateTimestamp
from hare.dialects.sqlite.functions.comparison.sqlite_greatest_least import SqliteGreatestLeast
from hare.dialects.sqlite.functions.datetime.date_truncation import DateTruncation
from hare.dialects.sqlite.functions.datetime.sqlite_date_part_extraction import SqliteDatePartExtraction
from hare.dialects.sqlite.functions.datetime.sqlite_local_now import SqliteLocalNow
from hare.dialects.sqlite.functions.datetime.sqlite_time_collation import SqliteTimeCollation
from hare.dialects.sqlite.functions.datetime.temporal_arithmetic_functions import TemporalArithmeticFunctions
from hare.dialects.sqlite.functions.decimal_storage import SqliteDecimalStorage
from hare.dialects.sqlite.functions.json.sqlite_json_containment import SqliteJsonContainment
from hare.dialects.sqlite.functions.json.sqlite_json_datetime import SqliteJsonDatetime
from hare.dialects.sqlite.functions.json.sqlite_json_equality import SqliteJsonEquality
from hare.dialects.sqlite.functions.json.sqlite_json_keys import SqliteJsonKeys
from hare.dialects.sqlite.functions.json.sqlite_json_ordering import SqliteJsonOrdering
from hare.dialects.sqlite.functions.json.sqlite_json_path import SqliteJsonPath
from hare.dialects.sqlite.functions.json.sqlite_json_values import SqliteJsonValues
from hare.dialects.sqlite.functions.math import SqliteMathFunctions
from hare.dialects.sqlite.functions.statistics.sqlite_statistics import SqliteStatistics
from hare.dialects.sqlite.functions.text import SqliteTextFunctions
from hare.dialects.sqlite.query.sqlite_query import SqliteQuery
from hare.exceptions import (
    ConfigurationError,
    DBConnectionError,
    HareError,
    IntegrityError,
    OperationalError,
    TooManyParametersError,
    TransactionManagementError,
)
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_call import QueryCall
from hare.instrumentation.query_tags import QueryTags
from hare.transactions.options import TransactionOptions

if TYPE_CHECKING:
    from hare.dialects.sqlite.client.sqlite_transaction_client import SqliteTransactionClient

T = TypeVar("T")


FuncType = Callable[..., Coroutine[None, None, T]]


# sqlite3 adapts parameters process-wide - raw queries and expressions too, where the field isn't known.
SqliteParameterAdapters.register()


class SqliteClient(DatabaseClient):
    driver_name = "sqlite"

    native_python_types = frozenset({bytes, str, int, float})
    query_class = SqliteQuery
    dialect = SQLITE_DIALECT

    @staticmethod
    def read_max_bind_parameters() -> int:
        """The bind-parameter ceiling of this process's sqlite3 build
        (``SQLITE_LIMIT_VARIABLE_NUMBER``), read off an in-memory connection - it sizes the batches
        of ``bulk_create()``/``bulk_update()``.

        Returns:
            The most parameters one statement may bind.
        """
        with closing(sqlite3.connect(SQLITE_IN_MEMORY_FILENAME)) as limit_probe_connection:
            return limit_probe_connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)

    @staticmethod
    def read_cascade_depth_limit() -> int:
        """The trigger recursion depth of this process's sqlite3 build
        (``SQLITE_LIMIT_TRIGGER_DEPTH``) - a native ``ON DELETE CASCADE`` stops there. Builds differ:
        the official 3.37.2 library stops at 100, newer ones at 1000.

        Returns:
            The depth.
        """
        with closing(sqlite3.connect(SQLITE_IN_MEMORY_FILENAME)) as limit_probe_connection:
            return limit_probe_connection.getlimit(sqlite3.SQLITE_LIMIT_TRIGGER_DEPTH)

    features = Features(
        inline_comments=True,
        supports_select_for_update=False,
        supports_update_limit_order_by=False,
        can_rollback_ddl=True,
        supports_returning=True,
        supports_positional_rows=True,
        max_bind_parameters=read_max_bind_parameters(),
        cascade_depth_limit=read_cascade_depth_limit(),
    )

    def __init__(self, file_path: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not isinstance(file_path, str) or not file_path:
            raise ConfigurationError(
                f"SQLite connection needs a non-empty file_path (a file name or ':memory:'), got {file_path!r}"
            )
        self.filename = file_path
        settings = {key: value for key, value in kwargs.items() if key not in NON_PRAGMA_KWARGS}
        self.install_regexp_functions: bool = SQLITE_REGEXP_OPTION.parse(kwargs.get(SQLITE_REGEXP_OPTION.name, False))
        defaults = {
            "journal_mode": SQLITE_DEFAULT_JOURNAL_MODE,
            "journal_size_limit": SQLITE_DEFAULT_JOURNAL_SIZE_LIMIT,
            "foreign_keys": SQLITE_DEFAULT_FOREIGN_KEYS,
            "case_sensitive_like": SQLITE_DEFAULT_CASE_SENSITIVE_LIKE,
        }
        #: PRAGMA name -> checked value, as sent to the connection.
        self.pragmas: dict[str, str | int] = {
            name: ("ON" if value else "OFF") if isinstance(value, bool) else value
            for name, value in SQLITE_CONNECTION_OPTIONS.read(settings, defaults).items()
        }
        SQLITE_CONNECTION_OPTIONS.raise_for_unknown(settings, "sqlite")

        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        #: The task running the top-level transaction that currently holds ``_lock``, if any.
        self._transaction_task: asyncio.Task[Any] | None = None

    async def get_server_version(self) -> tuple[int, ...]:
        return sqlite3.sqlite_version_info

    async def _post_connect(self) -> None:
        await super()._post_connect()
        # The date part, case mapping and date arithmetic functions are always installed - they back
        # lookups and functions every dialect has. The regex functions are opt-in.
        if self._connection:
            await SqliteDatePartExtraction.install(self._connection)
            await DateTruncation.install(self._connection)
            await SqliteMathFunctions.install(self._connection)
            await SqliteTextFunctions.install(self._connection)
            await SqliteCast.install(self._connection)
            await SqliteGreatestLeast.install(self._connection)
            await SqliteDateTimestamp.install(self._connection)
            await SqliteStatistics.install(self._connection)
            await SqliteCaseMapping.install_upper(self._connection)
            await SqliteCaseMapping.install_lower(self._connection)
            await TemporalArithmeticFunctions.install(self._connection)
            await SqliteJsonEquality.install(self._connection)
            await SqliteJsonPath.install(self._connection)
            await SqliteJsonKeys.install(self._connection)
            await SqliteJsonContainment.install(self._connection)
            await SqliteJsonDatetime.install(self._connection)
            await SqliteJsonValues.install(self._connection)
            await SqliteJsonOrdering.install(self._connection)
            await SqliteNumberText.install(self._connection)
            await SqliteDecimalCollation.install(self._connection)
            await SqliteDecimalStorage.install(self._connection)
            await SqliteTimeCollation.install(self._connection)
            await SqliteLocalNow.install(self._connection)

    async def create_connection(self, with_db: bool) -> None:
        if not self._connection:  # pragma: no branch
            connection = aiosqlite.connect(self.filename, isolation_level=None)
            # aiosqlite runs each connection on a thread of its own, started by the await below. A
            # daemon thread lets the interpreter exit when a program ends - on an exception, say -
            # without closing its connections; SQLite's journal rolls an unfinished transaction back.
            connection._thread.daemon = True
            self._connection = await connection
            self._connection._conn.row_factory = sqlite3.Row
            # A double-quoted name that is no column isn't read as a string literal - SQLite
            # otherwise builds an index or a condition on a constant instead of refusing the name.
            for double_quoted_strings_option in (sqlite3.SQLITE_DBCONFIG_DQS_DDL, sqlite3.SQLITE_DBCONFIG_DQS_DML):
                await self._connection._execute(  # type: ignore[no-untyped-call]
                    self._connection._conn.setconfig, double_quoted_strings_option, False
                )
            # One script for all pragmas - each aiosqlite call is a round trip through its worker
            # thread. Every name and value was validated: a whitelisted name and a keyword, ON/OFF
            # or an int.
            pragma_script = "".join(f"PRAGMA {pragma}={val};" for pragma, val in self.pragmas.items())
            if pragma_script:
                await self._connection.executescript(pragma_script)
            await self._post_connect()
            self.log.debug(
                "Created connection %s with params: filename=%s %s",
                self._connection,
                self.filename,
                " ".join(f"{k}={v}" for k, v in self.pragmas.items()),
            )

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
            self.log.warning("Closing connection %s while a transaction or query still uses it", self.connection_name)
            await self._close_unlocked()
            return
        try:
            await self._close_unlocked()
        finally:
            self._lock.release()

    async def _close_unlocked(self) -> None:
        if self._connection:
            await self._connection.close()
            self.log.debug(
                "Closed connection %s with params: filename=%s %s",
                self._connection,
                self.filename,
                " ".join(f"{k}={v}" for k, v in self.pragmas.items()),
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
        except OSError as e:
            if e.errno != errno.EINVAL:  # fix: "sqlite://:memory:" in Windows
                raise e

    def acquire_connection(self) -> ConnectionWrapper[aiosqlite.Connection]:
        return SqliteConnectionWrapper(self._lock, self)

    def _get_transaction_client(self) -> SqliteTransactionClient:
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.client.sqlite_transaction_client import SqliteTransactionClient

        return SqliteTransactionClient(self)

    def _get_transaction_restriction_statements(self, options: TransactionOptions) -> list[str]:
        """``PRAGMA query_only = ON`` for a read-only transaction - the statement timeout needs no
        statement, SqliteTransactionClient interrupts an over-running query itself, and the
        isolation level none, every SQLite transaction being serializable.

        Args:
            options: The transaction's options.

        Returns:
            The statements to run right after BEGIN, in order.
        """
        statements = self._get_isolation_statements(options)
        if options.read_only:
            statements.append(SQLITE_ENABLE_QUERY_ONLY_SQL)
        return statements

    @staticmethod
    def _is_read_only_transaction_write(client: Any, error: sqlite3.OperationalError) -> bool:
        """Whether ``error`` is SQLite refusing a write because the transaction is read-only
        (``PRAGMA query_only``).

        Args:
            client: The client the statement ran on.
            error: The error.

        Returns:
            True for a write refused in a read-only transaction.
        """
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.client.sqlite_transaction_client import SqliteTransactionClient

        return (
            isinstance(client, SqliteTransactionClient)
            and client._transaction_options.read_only
            and getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_READONLY
        )

    @staticmethod
    def translate_exceptions(func: FuncType[Any]) -> FuncType[Any]:
        """Wraps a client method: appends the query tags to its SQL, runs it inside the query
        wrappers, raises sqlite3's exceptions as hare's and reports the query to the observers.

        Args:
            func: The method.

        Returns:
            The wrapped method.
        """

        def get_translating_method(wrapped_method: FuncType[Any] | None) -> FuncType[Any]:
            # wrapped_method: the method the query wrappers run, its SQL tagged already - None
            # for that method itself.
            method_name = func.__name__
            is_query_executing = method_name in QUERY_EXECUTING_METHOD_NAMES
            is_observed = method_name in OBSERVED_METHOD_NAMES
            tags_sql = wrapped_method is not None
            runs_query_wrappers = is_observed and wrapped_method is not None

            async def translating_method(self: Any, *args: Any, **kwargs: Any) -> Any:
                if is_query_executing and not (tags_sql and QueryTags.current.get()):
                    sql = args[0] if args else kwargs.get("query")
                    params = args[1] if len(args) > 1 else kwargs.get("values")
                else:
                    args, kwargs, sql, params = DatabaseClient.get_tagged_query_arguments(args, kwargs, method_name)
                if runs_query_wrappers and Observers.query_wrappers:
                    call = QueryCall(method_name, cast("str", sql), params, self.connection_name, self.dialect)
                    return await Observers.run_wrapped(
                        call, partial(cast("FuncType[Any]", wrapped_method), self, *args, **kwargs)
                    )
                start = time.monotonic()
                exception: Exception | None = None
                try:
                    # A stale reference to a finished transaction or savepoint must not run a query - a
                    # row inserted after rollback() would be kept. A transaction client of this dialect
                    # is always the SQLite one.
                    if is_query_executing and self.is_transaction_client:
                        self._check_statement_allowed()
                        if self._is_transaction_aborted():
                            exception = TransactionManagementError(SQLITE_TRANSACTION_ABORTED_MESSAGE)
                            raise exception
                    return await func(self, *args, **kwargs)
                except sqlite3.OperationalError as exc:
                    # A narrower, catchable type for exactly this one message - Model.delete()/
                    # QuerySet.delete() retry with a Python-side cascade walk on it instead of a
                    # native ON DELETE CASCADE that can't finish past SQLITE_LIMIT_TRIGGER_DEPTH.
                    if SQLITE_TRIGGER_RECURSION_LIMIT_MESSAGE in str(exc):
                        exception = SqliteTriggerRecursionLimitError(exc, sql=sql, params=params)
                    elif SQLITE_TOO_MANY_VARIABLES_MESSAGE in str(exc):
                        exception = TooManyParametersError(exc, sql=sql, params=params)
                    elif any(message in str(exc) for message in SQLITE_TOO_MANY_JOINED_TABLES_MESSAGES):
                        exception = OperationalError(
                            f"{exc} - the query joins more tables than SQLite allows in one statement. Every "
                            "relation hop in a filter()/exclude()/order_by()/values()/select_related() path adds a "
                            "JOIN (a many-to-many hop two); shorten the relation paths.",
                            sql=sql,
                            params=params,
                        )
                    elif SqliteClient._is_read_only_transaction_write(self, exc):
                        # The same error PostgreSQL gives a write in a read-only transaction.
                        exception = TransactionManagementError(f"cannot write in a read-only transaction: {exc}")
                    else:
                        exception = SqliteClient._get_operational_error(exc, sql=sql, params=params)
                    raise exception from exc
                except sqlite3.IntegrityError as exc:
                    exception = IntegrityError(exc, sql=sql, params=params)
                    raise exception from exc
                # InterfaceError means the connection or cursor is unusable - a DBConnectionError. The
                # other sqlite3 errors (ProgrammingError, DataError, InternalError, NotSupportedError)
                # are OperationalError.
                except sqlite3.InterfaceError as exc:
                    exception = DBConnectionError(exc, sql=sql, params=params)
                    raise exception from exc
                except (
                    sqlite3.ProgrammingError,
                    sqlite3.DataError,
                    sqlite3.InternalError,
                    sqlite3.NotSupportedError,
                ) as exc:
                    exception = OperationalError(exc, sql=sql, params=params)
                    raise exception from exc
                # A bare sqlite3.DatabaseError - a corrupted database file raises it.
                except sqlite3.DatabaseError as exc:
                    exception = OperationalError(exc, sql=sql, params=params)
                    raise exception from exc
                except HareError as exc:
                    exception = exc
                    raise
                # sqlite3 raises these outside its own Error hierarchy while binding a query parameter -
                # an int beyond int64 (OverflowError), or a value of a type it can't adapt. Left as-is
                # for commit()/rollback(), whose on-commit callbacks are application code.
                except (OverflowError, ValueError, TypeError) as exc:
                    if isinstance(exc, ValueError) and str(exc) == AIOSQLITE_NO_ACTIVE_CONNECTION_MESSAGE:
                        exception = DBConnectionError(exc, sql=sql, params=params)
                        raise exception from exc
                    if not is_query_executing:
                        exception = exc
                        raise
                    exception = OperationalError(exc, sql=sql, params=params)
                    raise exception from exc
                except Exception as exc:
                    exception = exc
                    raise
                finally:
                    if is_observed:
                        Observers.record_query(sql, params, start, exception, self.connection_name)

            return translating_method

        return wraps(func)(get_translating_method(get_translating_method(None)))

    @translate_exceptions
    async def execute_many(self, query: str, values: list[list[Any]]) -> None:
        async with self.acquire_connection() as connection:
            self.log.debug("%s: %s", query, values)
            # This code is only ever called in AUTOCOMMIT mode
            await connection.execute("BEGIN")
            try:
                await connection.executemany(query, values)
            except Exception:
                await connection.rollback()
                raise
            else:
                await connection.commit()

    @staticmethod
    def _escape_null_bytes(query: str) -> str:
        return query.replace(SQLITE_NULL_BYTE, SQLITE_NULL_BYTE_ESCAPE)

    @staticmethod
    def _run_on_worker_thread(
        connection: aiosqlite.Connection, function: Callable[..., T], *args: Any
    ) -> Coroutine[Any, Any, T]:
        """Runs ``function`` with the underlying sqlite3 connection and ``args`` in a single hop to
        aiosqlite's worker thread.

        Args:
            connection: The aiosqlite connection.
            function: Called as ``function(sqlite3_connection, *args)`` on the worker thread.
            args: The remaining arguments.

        Returns:
            The coroutine returning what ``function`` returned.
        """
        run_on_worker_thread: Callable[..., Coroutine[Any, Any, T]] = connection._execute
        return run_on_worker_thread(function, connection._conn, *args)

    @staticmethod
    def _fetch_rows_and_row_count(
        raw_connection: sqlite3.Connection, query: str, values: list[Any] | None
    ) -> tuple[list[sqlite3.Row], int, int | None]:
        """Runs one statement on aiosqlite's worker thread and returns its rows, the rows the
        statement itself changed - never rows changed by triggers or foreign-key actions it set
        off - and the row id of the row an INSERT added.

        Args:
            raw_connection: The underlying sqlite3 connection.
            query: The SQL statement.
            values: Its positional parameters.

        Returns:
            The fetched rows, the affected-row count (the row count for a non-writing statement)
            and the last inserted row id.
        """
        total_changes_before = raw_connection.total_changes
        cursor = raw_connection.execute(query, values or [])
        try:
            rows = cursor.fetchall()
            row_count = cursor.rowcount
            inserted_id = cursor.lastrowid
        finally:
            cursor.close()
        if row_count >= 0:
            return rows, row_count, inserted_id
        # sqlite3 reports -1 for a write it doesn't recognise by its leading keyword (a
        # WITH-prefixed one) - changes() still counts only that statement's own rows.
        if raw_connection.total_changes != total_changes_before:
            return rows, raw_connection.execute(SQLITE_LAST_STATEMENT_CHANGES_SQL).fetchone()[0], inserted_id
        return rows, len(rows), inserted_id

    @translate_exceptions
    async def execute(
        self, query: str, values: list[Any] | None = None, *, returns_rows: bool | None = None
    ) -> StatementResult:
        if SQLITE_NULL_BYTE in query:
            query = self._escape_null_bytes(query)
        async with self.acquire_connection() as connection:
            self.log.debug("%s: %s", query, values)
            # One worker-thread hop for execute + fetch + row count, like execute_fetchall().
            rows, row_count, inserted_id = await self._run_on_worker_thread(
                connection, self._fetch_rows_and_row_count, query, values
            )
            # sqlite3.Row reads both by position and by name (Features.supports_positional_rows).
            return StatementResult(row_count, rows, inserted_id)

    @staticmethod
    def _fetch_described_rows(
        raw_connection: sqlite3.Connection, query: str, values: list[Any] | None
    ) -> tuple[tuple[str, ...], list[sqlite3.Row], int]:
        """``_fetch_rows_and_row_count()`` with the statement's column names first.

        Args:
            raw_connection: The underlying sqlite3 connection.
            query: The SQL statement.
            values: Its positional parameters.

        Returns:
            The column names, the fetched rows and the affected-row count.
        """
        total_changes_before = raw_connection.total_changes
        cursor = raw_connection.execute(query, values or [])
        try:
            columns = tuple(column[0] for column in cursor.description or ())
            rows = cursor.fetchall()
            row_count = cursor.rowcount
        finally:
            cursor.close()
        if row_count < 0:
            # See _fetch_rows_and_row_count() - a WITH-prefixed write reports -1.
            if raw_connection.total_changes != total_changes_before:
                row_count = raw_connection.execute(SQLITE_LAST_STATEMENT_CHANGES_SQL).fetchone()[0]
            else:
                row_count = len(rows)
        return columns, rows, row_count

    @translate_exceptions
    async def execute_described(self, query: str, values: list[Any] | None = None) -> DescribedResult:
        if SQLITE_NULL_BYTE in query:
            query = self._escape_null_bytes(query)
        async with self.acquire_connection() as connection:
            self.log.debug("%s: %s", query, values)
            columns, rows, row_count = await self._run_on_worker_thread(
                connection, self._fetch_described_rows, query, values
            )
            return DescribedResult(columns=columns, rows=tuple(tuple(row) for row in rows), row_count=row_count)

    @translate_exceptions
    async def execute_script(self, query: str) -> None:
        async with self.acquire_connection() as connection:
            self.log.debug(query)
            await connection.executescript(query)

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

    @staticmethod
    def _execute_statements(raw_connection: sqlite3.Connection, statements: list[str]) -> None:
        """Runs ``statements`` one by one on aiosqlite's worker thread.

        Args:
            raw_connection: The underlying sqlite3 connection.
            statements: The SQL statements, in order.
        """
        for statement in statements:
            raw_connection.execute(statement).close()
