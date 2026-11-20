from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
import urllib.parse as urlparse
from collections.abc import Callable, Coroutine
from contextlib import closing
from typing import TYPE_CHECKING, Any, TypeVar

import aiosqlite

from hare.core.resource_completion import ResourceCompletion
from hare.dialects.base.client.connect_failure_reports import ConnectFailureReports
from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.client.driver_statement_log_filter import DriverStatementLogFilter
from hare.dialects.base.client.enums import StatementForm
from hare.dialects.base.results.described_result import DescribedResult
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.sqlite.client.constants import SQLITE_ATTACH_DATABASE_SQL
from hare.dialects.sqlite.client.declarations import SqliteDriverErrors
from hare.dialects.sqlite.client.sqlite_client import SqliteClient
from hare.dialects.sqlite.connection.constants import SQLITE_PRIMARY_RESULT_CODE_MASK
from hare.dialects.sqlite.constants import (
    SQLITE_AIOSQLITE_DRIVER_NAME,
    SQLITE_DATABASE_FILE_EXISTS_MESSAGE,
    SQLITE_DATABASE_LIST_SQL,
    SQLITE_IN_MEMORY_FILENAME,
)
from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_connection_wrapper import AiosqliteConnectionWrapper
from hare.dialects.sqlite.drivers.aiosqlite.constants import (
    AIOSQLITE_CALL_LOG_FORMATS,
    AIOSQLITE_LIBRARY_LOGGER_NAME,
    AIOSQLITE_NO_ACTIVE_CONNECTION_MESSAGE,
)
from hare.dialects.sqlite.drivers.constants import (
    SQLITE_FIRST_NUMBERED_PLACEHOLDER,
    SQLITE_FTS5_COMPILE_OPTION_SQL,
    SQLITE_LAST_STATEMENT_CHANGES_SQL,
    SQLITE_OPEN_FAILED_MESSAGE,
    SQLITE_SETUP_RELEASE_FAILED,
)
from hare.dialects.sqlite.enums import SqliteStorageState
from hare.dialects.sqlite.functions.comparison.sqlite_cast import SqliteCast
from hare.dialects.sqlite.functions.comparison.sqlite_greatest_least import SqliteGreatestLeast
from hare.dialects.sqlite.functions.datetime.sqlite_date_timestamp import SqliteDateTimestamp
from hare.dialects.sqlite.functions.datetime.sqlite_local_now import SqliteLocalNow
from hare.dialects.sqlite.functions.datetime.temporal_arithmetic_functions import TemporalArithmeticFunctions
from hare.dialects.sqlite.functions.json.sqlite_json_datetime import SqliteJsonDatetime
from hare.dialects.sqlite.functions.json.sqlite_json_path import SqliteJsonPath
from hare.dialects.sqlite.functions.sqlite_math_functions import SqliteMathFunctions
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.dialects.sqlite.functions.statistics.sqlite_statistics import SqliteStatistics
from hare.dialects.sqlite.functions.text.sqlite_text_functions import SqliteTextFunctions
from hare.dialects.sqlite.search.sqlite_full_text_query import SqliteFullTextQuery
from hare.dialects.sqlite.server_versions import (
    SQLITE_STRICT_SERVER_VERSION,
    SQLITE_UNHEX_SERVER_VERSION,
)
from hare.dialects.sqlite.transactions.constants import SQLITE_BUSY_TIMEOUT_SQL, SQLITE_SET_BUSY_TIMEOUT_SQL
from hare.dialects.sqlite.vectors.sqlite_vector_extension import SqliteVectorExtension
from hare.exceptions import DBConnectionError, OperationalError

if TYPE_CHECKING:
    from hare.dialects.sqlite.declarations import SqliteFeatures
    from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_transaction_client import (
        AiosqliteTransactionClient,
    )

T = TypeVar("T")


class AiosqliteClient(SqliteClient):
    """SQLite through aiosqlite: the stdlib ``sqlite3`` module's connection, run on a thread of its
    own - every statement is one hop to that thread."""

    @staticmethod
    def _read_features() -> SqliteFeatures:
        """Read limits and features of the linked SQLite library from one probe."""
        with closing(sqlite3.connect(SQLITE_IN_MEMORY_FILENAME)) as probe:
            return SqliteClient.features.replace(
                supports_positional_rows=True,
                max_bind_parameters=probe.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER),
                cascade_depth_limit=probe.getlimit(sqlite3.SQLITE_LIMIT_TRIGGER_DEPTH),
                supports_unhex=sqlite3.sqlite_version_info >= SQLITE_UNHEX_SERVER_VERSION,
                supports_strict_tables=sqlite3.sqlite_version_info >= SQLITE_STRICT_SERVER_VERSION,
                supports_full_text_index=bool(probe.execute(SQLITE_FTS5_COMPILE_OPTION_SQL).fetchone()[0]),
            )

    driver_name = SQLITE_AIOSQLITE_DRIVER_NAME
    features = _read_features()
    driver_errors = SqliteDriverErrors(
        operational=sqlite3.OperationalError,
        integrity=sqlite3.IntegrityError,
        interface=sqlite3.InterfaceError,
        statement=(sqlite3.ProgrammingError, sqlite3.DataError, sqlite3.InternalError, sqlite3.NotSupportedError),
        database=sqlite3.DatabaseError,
        closed_connection_message=AIOSQLITE_NO_ACTIVE_CONNECTION_MESSAGE,
    )

    async def swap_busy_timeout(self, connection: aiosqlite.Connection, milliseconds: int) -> int:
        async with connection.execute(SQLITE_BUSY_TIMEOUT_SQL) as cursor:
            row = await cursor.fetchone()
        await connection.execute(SQLITE_SET_BUSY_TIMEOUT_SQL.format(milliseconds=milliseconds))
        return int(row[0]) if row is not None else 0

    async def get_server_version(self) -> tuple[int, ...]:
        return sqlite3.sqlite_version_info

    async def _post_connect(self) -> None:
        await super()._post_connect()
        # The date part and date arithmetic functions are always installed - they back
        # lookups and functions every dialect has. The regex functions are opt-in.
        if self._connection:
            await SqliteNativeFunctions.install_dates(self._connection, self.function_errors)
            await SqliteMathFunctions.install(self._connection, self.function_errors)
            await SqliteTextFunctions.install(self._connection, self.function_errors)
            await SqliteCast.install(self._connection, self.function_errors)
            await SqliteGreatestLeast.install(self._connection)
            await SqliteDateTimestamp.install(self._connection)
            await SqliteStatistics.install(self._connection)
            await TemporalArithmeticFunctions.install(self._connection, self.function_errors)
            await SqliteJsonPath.install(self._connection)
            await SqliteJsonDatetime.install(self._connection)
            await SqliteLocalNow.install(self._connection)
            await SqliteFullTextQuery.install(self._connection)
            if self.load_sqlite_vec:
                await SqliteVectorExtension.install(self._connection)
            if self.spatial_extension is not None:
                spatial_reference_ids = await self.spatial_extension.install(self._connection)
                self.features = self.features.replace(spatial_reference_ids=spatial_reference_ids)

    @classmethod
    async def _await_opened_connection(cls, connection: aiosqlite.Connection) -> aiosqlite.Connection:
        """Finish acquisition before cancellation, closing any acquired resource before raising.

        Cancelling aiosqlite's connect future can discard a connection created later by its
        worker. Drain that future first; repeated cancellation cannot detach its cleanup.
        """
        try:
            return await ResourceCompletion.wait(connection)
        except BaseException as error:
            await cls._close_failed_connection(connection, error)
            raise

    @staticmethod
    async def _close_failed_connection(connection: aiosqlite.Connection, error: BaseException) -> None:
        """Drain failed acquisition or setup cleanup, preserving real errors and cancellation.

        Pure repeated cancellation remains cancellation; an independent driver failure is
        retained with the original error. The driver remains responsible for physical close.
        """

        async def release() -> None:
            await connection.close()
            await asyncio.to_thread(connection._thread.join)

        try:
            await ResourceCompletion.wait(release())
        except BaseException as release_error:
            if isinstance(error, asyncio.CancelledError) and isinstance(release_error, asyncio.CancelledError):
                raise error from None
            raise BaseExceptionGroup(SQLITE_SETUP_RELEASE_FAILED, [error, release_error]) from None

    async def _get_native_storage_filename(self, connection: aiosqlite.Connection) -> str:
        """Read main storage before setup hooks; attached databases are not deletion targets."""

        def read(raw_connection: sqlite3.Connection) -> str:
            with closing(raw_connection.cursor()) as cursor:
                cursor.row_factory = None
                cursor.execute(SQLITE_DATABASE_LIST_SQL)
                return next(row[2] for row in cursor.fetchall() if row[1] == "main")

        return await self._run_on_worker_thread(connection, read)

    async def _probe_storage_filename(self, identifier: str, *, uri: bool = False) -> str:
        """Observe native filename handling without setup hooks or connection PRAGMAs."""
        options: dict[str, Any] = {"uri": True} if uri else {}
        connection = aiosqlite.connect(identifier, isolation_level=None, **options)
        connection._thread.daemon = True
        opened = await self._await_opened_connection(connection)
        try:
            return await self._get_native_storage_filename(opened)
        finally:
            await ResourceCompletion.wait(connection.close())

    async def _get_unopened_storage_filename(self) -> str:
        if self.filename == SQLITE_IN_MEMORY_FILENAME:
            return ""
        if not self.filename.startswith("file:"):
            return os.path.abspath(self.filename)
        return await self._probe_storage_filename(self.filename)

    async def db_create(self) -> None:
        """Refuse an existing native or literal URI target without guessing its parsed filename.

        An existing-only URI probe supplies the comparison target. The ordinary driver defaults
        determine whether the configured identifier actually addresses that URI or a literal
        file. Only the observed storage is retained; neither probe runs user setup hooks.
        """
        if self._connection is not None:
            if self._storage_filename:
                raise OperationalError(SQLITE_DATABASE_FILE_EXISTS_MESSAGE.format(filename=self._storage_filename))
            if self._storage_filename == "":
                return
        if not self.filename.startswith("file:"):
            await super().db_create()
            return
        # Preserve opaque filename text. urlunsplit/geturl changes a relative file: URI.
        without_fragment, fragment_separator, fragment = self.filename.partition("#")
        prefix, _query_separator, query = without_fragment.partition("?")
        parameters = urlparse.parse_qsl(query.replace("+", "%2B"), keep_blank_values=True, errors="surrogateescape")
        mode = next((value for key, value in parameters if key == "mode"), None)
        if mode != "memory":
            parameters = [(key, value) for key, value in parameters if key != "mode"]
            parameters.append(("mode", "ro"))
        existing_identifier = (
            prefix + "?" + urlparse.urlencode(parameters, quote_via=urlparse.quote, errors="surrogateescape")
        )
        if fragment_separator:
            existing_identifier += "#" + fragment
        raw_filename = os.path.realpath(self.filename)
        raw_existed = os.path.exists(raw_filename)
        existing_filename = None
        existing_error = None
        try:
            existing_filename = await self._probe_storage_filename(existing_identifier, uri=True)
        except sqlite3.Error as error:
            existing_error = error
        previous_identity = self._storage_state, self._storage_filename
        self._storage_state, self._storage_filename = SqliteStorageState.OPENING, None
        try:
            actual_filename = await self._probe_storage_filename(self.filename)
        except sqlite3.Error as error:
            raise OperationalError(str(error)) from error
        if actual_filename:
            if actual_filename == existing_filename or actual_filename == raw_filename and raw_existed:
                self._storage_state, self._storage_filename = previous_identity
                raise OperationalError(SQLITE_DATABASE_FILE_EXISTS_MESSAGE.format(filename=actual_filename))
            if actual_filename != raw_filename and existing_error is not None:
                error_code = getattr(existing_error, "sqlite_errorcode", None)
                if error_code is None or error_code & SQLITE_PRIMARY_RESULT_CODE_MASK != sqlite3.SQLITE_CANTOPEN:
                    raise OperationalError(str(existing_error)) from existing_error
        self._storage_state, self._storage_filename = SqliteStorageState.OBSERVED, actual_filename

    async def create_connection(self, with_db: bool) -> None:
        DriverStatementLogFilter.install(AIOSQLITE_LIBRARY_LOGGER_NAME, AIOSQLITE_CALL_LOG_FORMATS)
        if not self._connection:
            start_time = time.perf_counter()
            self._storage_state, self._storage_filename = SqliteStorageState.OPENING, None
            connection = aiosqlite.connect(self.filename, isolation_level=None)
            # aiosqlite runs each connection on a thread of its own, started by the await below. A
            # daemon thread lets the interpreter exit when a program ends - on an exception, say -
            # without closing its connections; SQLite's journal rolls an unfinished transaction back.
            connection._thread.daemon = True
            try:
                opened_connection = self._connection = await self._await_opened_connection(connection)
            except (sqlite3.Error, OSError) as error:
                ConnectFailureReports.record(self, error)
                raise DBConnectionError(
                    SQLITE_OPEN_FAILED_MESSAGE.format(filename=self.filename, error=error)
                ) from error
            try:
                filename = await self._get_native_storage_filename(opened_connection)
                self._storage_state, self._storage_filename = SqliteStorageState.OBSERVED, filename
                await self._set_up_connection(opened_connection)
            except BaseException as error:
                # A connection whose setup failed - a PRAGMA, an extension that didn't load - isn't
                # kept open.
                try:
                    await self._close_failed_connection(opened_connection, error)
                finally:
                    self._connection = None
                raise
            self.pool_statistics.add_connect(time.perf_counter() - start_time)
            self.log.debug(
                "Created connection %s with params: filename=%s %s",
                opened_connection,
                self.filename,
                " ".join(f"{pragma}={value}" for pragma, value in self.pragmas.items()),
            )

    async def _set_up_connection(self, connection: aiosqlite.Connection) -> None:
        """Configures a just-opened connection: rows, double-quoted names, the PRAGMAs, the attached
        databases, then the functions and extensions ``_post_connect()`` installs.

        Args:
            connection: The connection.
        """
        # Rows are plain tuples by default - a read by name takes a cursor of sqlite3.Row rows.
        connection._conn.row_factory = None
        # A double-quoted name that is no column isn't read as a string literal - SQLite otherwise
        # builds an index or a condition on a constant instead of refusing the name.
        for double_quoted_strings_option in (sqlite3.SQLITE_DBCONFIG_DQS_DDL, sqlite3.SQLITE_DBCONFIG_DQS_DML):
            await connection._execute(  # type: ignore[no-untyped-call]
                connection._conn.setconfig, double_quoted_strings_option, False
            )
        # One script for all pragmas - each aiosqlite call is a round trip through its worker thread.
        # Every name and value was validated: a whitelisted name and a keyword, ON/OFF or an int.
        pragma_script = "".join(f"PRAGMA {pragma}={value};" for pragma, value in self.pragmas.items())
        if pragma_script:
            await connection.executescript(pragma_script)
        self.reset_foreign_key_state()
        literals = self.dialect.literals
        attach_script = "".join(
            SQLITE_ATTACH_DATABASE_SQL.format(
                file=literals.get_string_literal_sql(file), name=literals.quote_identifier(name)
            )
            for name, file in self.attached_databases.items()
        )
        if attach_script:
            await connection.executescript(attach_script)
        await self._post_connect()

    def acquire_connection(self) -> ConnectionWrapper[aiosqlite.Connection]:
        # A client Hare.init replaced runs a query that took it on the one replacing it.
        if (successor := self.get_successor()) is not None:
            return successor.acquire_connection()  # type: ignore[return-value]
        return AiosqliteConnectionWrapper(self._lock, self)

    def _get_transaction_client(self) -> AiosqliteTransactionClient:
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_transaction_client import (
            AiosqliteTransactionClient,
        )

        return AiosqliteTransactionClient(self)

    def _uses_numbered_parameters(self, query: str) -> bool:
        return self.binds_parameters_by_number and SQLITE_FIRST_NUMBERED_PLACEHOLDER in query

    def _get_bound_parameters(self, query: str, values: list[Any] | None) -> list[Any] | dict[str, Any]:
        bound = self.parameter_adapters.bind(values)
        return self.get_values_by_number(bound) if bound and self._uses_numbered_parameters(query) else bound

    def _get_many_parameters(self, query: str, values: list[list[Any]]) -> list[list[Any]] | list[dict[str, Any]]:
        bound = [self.parameter_adapters.bind(row) for row in values]
        return [self.get_values_by_number(row) for row in bound] if self._uses_numbered_parameters(query) else bound

    @SqliteClient.translate_exceptions(StatementForm.MANY)
    async def execute_many(self, query: str, values: list[list[Any]]) -> None:
        async with self.acquire_connection() as connection:
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug("%s: %s", query, values)
            bound = self._get_many_parameters(query, values)
            # This code is only ever called in AUTOCOMMIT mode
            await connection.execute("BEGIN")
            try:
                await connection.executemany(query, bound)
            except Exception:
                await connection.rollback()
                raise
            else:
                await connection.commit()

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
        raw_connection: sqlite3.Connection,
        query: str,
        values: list[Any] | dict[str, Any] | None,
        row_factory: type[sqlite3.Row] | None,
    ) -> tuple[list[Any], int, int | None, tuple[tuple[Any, ...], ...] | None]:
        """Runs one statement on aiosqlite's worker thread and returns its rows, the rows the
        statement itself changed - never rows changed by triggers or foreign-key actions it set
        off - the row id of the row an INSERT added and the cursor's description.

        Args:
            raw_connection: The underlying sqlite3 connection.
            query: The SQL statement.
            values: Its positional parameters.
            row_factory: ``sqlite3.Row`` for rows read by name too, None for plain tuples.

        Returns:
            The fetched rows, the affected-row count (the row count for a non-writing statement),
            the last inserted row id and the description.
        """
        total_changes_before = raw_connection.total_changes
        if row_factory is None:
            cursor = raw_connection.execute(query, values or [])
        else:
            cursor = raw_connection.cursor()
            cursor.row_factory = row_factory
            cursor.execute(query, values or [])
        try:
            rows = cursor.fetchall()
            row_count = cursor.rowcount
            inserted_id = cursor.lastrowid
            description = cursor.description
        finally:
            cursor.close()
        if row_count >= 0:
            return rows, row_count, inserted_id, description
        # sqlite3 reports -1 for a write it doesn't recognise by its leading keyword (a
        # WITH-prefixed one) - changes() still counts only that statement's own rows.
        if raw_connection.total_changes != total_changes_before:
            return (
                rows,
                raw_connection.execute(SQLITE_LAST_STATEMENT_CHANGES_SQL).fetchone()[0],
                inserted_id,
                description,
            )
        return rows, len(rows), inserted_id, description

    @SqliteClient.translate_exceptions(StatementForm.SQL)
    async def execute(
        self,
        query: str,
        values: list[Any] | None = None,
        *,
        returns_rows: bool | None = None,
        rows_by_position: bool = False,
        retry_read: bool = False,
    ) -> StatementResult:
        async with self.acquire_connection() as connection:
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug("%s: %s", query, values)
            bound = self._get_bound_parameters(query, values)
            # One worker-thread hop for execute + fetch + row count, like execute_fetchall(). Rows
            # read by position, and the no rows of a write, are plain tuples - no sqlite3.Row built
            # per row; sqlite3.Row reads both by position and by name
            # (Features.supports_positional_rows).
            rows, row_count, inserted_id, description = await self._run_on_worker_thread(
                connection,
                self._fetch_rows_and_row_count,
                query,
                bound,
                None if rows_by_position or returns_rows is False else sqlite3.Row,
            )
            return StatementResult(row_count, rows, inserted_id, description)

    @SqliteClient.translate_exceptions(StatementForm.SQL)
    async def execute_described(self, query: str, values: list[Any] | None = None) -> DescribedResult:
        async with self.acquire_connection() as connection:
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug("%s: %s", query, values)
            bound = self._get_bound_parameters(query, values)
            rows, row_count, _, description = await self._run_on_worker_thread(
                connection, self._fetch_rows_and_row_count, query, bound, None
            )
            columns = tuple(column[0] for column in description or ())
            return DescribedResult(columns=columns, rows=tuple(tuple(row) for row in rows), row_count=row_count)

    @SqliteClient.translate_exceptions(StatementForm.SCRIPT)
    async def execute_script(self, query: str) -> None:
        async with self.acquire_connection() as connection:
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug(query)
            await connection.executescript(query)

    @staticmethod
    def _execute_statements(raw_connection: sqlite3.Connection, statements: list[str]) -> None:
        """Runs ``statements`` one by one on aiosqlite's worker thread.

        Args:
            raw_connection: The underlying sqlite3 connection.
            statements: The SQL statements, in order.
        """
        for statement in statements:
            raw_connection.execute(statement).close()
