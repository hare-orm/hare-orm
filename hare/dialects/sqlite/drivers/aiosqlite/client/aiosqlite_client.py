from __future__ import annotations

import datetime
import logging
import sqlite3
import time
from collections.abc import Callable, Coroutine
from contextlib import closing
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

import aiosqlite

from hare.dialects.base.client.connect_failure_reports import ConnectFailureReports
from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.results.described_result import DescribedResult
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.sqlite.client.declarations import SqliteDriverErrors
from hare.dialects.sqlite.client.sqlite_client import SqliteClient
from hare.dialects.sqlite.constants import SQLITE_AIOSQLITE_DRIVER_NAME, SQLITE_IN_MEMORY_FILENAME, SQLITE_NULL_BYTE
from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_connection_wrapper import AiosqliteConnectionWrapper
from hare.dialects.sqlite.drivers.aiosqlite.constants import AIOSQLITE_NO_ACTIVE_CONNECTION_MESSAGE
from hare.dialects.sqlite.drivers.constants import (
    SQLITE_FIRST_NUMBERED_PLACEHOLDER,
    SQLITE_FTS5_COMPILE_OPTION_SQL,
    SQLITE_LAST_STATEMENT_CHANGES_SQL,
    SQLITE_OPEN_FAILED_MESSAGE,
)
from hare.dialects.sqlite.functions.comparison.sqlite_cast import SqliteCast
from hare.dialects.sqlite.functions.comparison.sqlite_greatest_least import SqliteGreatestLeast
from hare.dialects.sqlite.functions.datetime.date_truncation import DateTruncation
from hare.dialects.sqlite.functions.datetime.sqlite_date_part_extraction import SqliteDatePartExtraction
from hare.dialects.sqlite.functions.datetime.sqlite_date_timestamp import SqliteDateTimestamp
from hare.dialects.sqlite.functions.datetime.sqlite_local_now import SqliteLocalNow
from hare.dialects.sqlite.functions.datetime.sqlite_time_collation import SqliteTimeCollation
from hare.dialects.sqlite.functions.datetime.temporal_arithmetic_functions import TemporalArithmeticFunctions
from hare.dialects.sqlite.functions.decimal.sqlite_decimal_collation import SqliteDecimalCollation
from hare.dialects.sqlite.functions.decimal.sqlite_decimal_storage import SqliteDecimalStorage
from hare.dialects.sqlite.functions.json.sqlite_json_containment import SqliteJsonContainment
from hare.dialects.sqlite.functions.json.sqlite_json_datetime import SqliteJsonDatetime
from hare.dialects.sqlite.functions.json.sqlite_json_equality import SqliteJsonEquality
from hare.dialects.sqlite.functions.json.sqlite_json_keys import SqliteJsonKeys
from hare.dialects.sqlite.functions.json.sqlite_json_ordering import SqliteJsonOrdering
from hare.dialects.sqlite.functions.json.sqlite_json_path import SqliteJsonPath
from hare.dialects.sqlite.functions.json.sqlite_json_values import SqliteJsonValues
from hare.dialects.sqlite.functions.sqlite_math_functions import SqliteMathFunctions
from hare.dialects.sqlite.functions.statistics.sqlite_statistics import SqliteStatistics
from hare.dialects.sqlite.functions.text.sqlite_case_mapping import SqliteCaseMapping
from hare.dialects.sqlite.functions.text.sqlite_number_text import SqliteNumberText
from hare.dialects.sqlite.functions.text.sqlite_text_functions import SqliteTextFunctions
from hare.dialects.sqlite.parameters.sqlite_parameter_adapters import SqliteParameterAdapters
from hare.dialects.sqlite.search.sqlite_full_text_query import SqliteFullTextQuery
from hare.dialects.sqlite.server_versions import (
    SQLITE_AUTOMATIC_INDEX_COLLATION_FAULT_SERVER_VERSIONS,
    SQLITE_DROP_COLUMN_SERVER_VERSION,
    SQLITE_RETURNING_FOREIGN_KEY_ERROR_FAULT_SERVER_VERSIONS,
    SQLITE_STRICT_SERVER_VERSION,
    SQLITE_UNHEX_SERVER_VERSION,
)
from hare.dialects.sqlite.transactions.constants import SQLITE_BUSY_TIMEOUT_SQL, SQLITE_SET_BUSY_TIMEOUT_SQL
from hare.dialects.sqlite.vectors.sqlite_vector_extension import SqliteVectorExtension
from hare.exceptions import DBConnectionError

if TYPE_CHECKING:
    from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_transaction_client import (
        AiosqliteTransactionClient,
    )

T = TypeVar("T")


class AiosqliteClient(SqliteClient):
    """SQLite through aiosqlite: the stdlib ``sqlite3`` module's connection, run on a thread of its
    own - every statement is one hop to that thread."""

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

    @staticmethod
    def read_full_text_support() -> bool:
        """Whether this process's sqlite3 build has the FTS5 full-text search module.

        Returns:
            True when ``CREATE VIRTUAL TABLE ... USING fts5`` works.
        """
        with closing(sqlite3.connect(SQLITE_IN_MEMORY_FILENAME)) as module_probe_connection:
            return bool(module_probe_connection.execute(SQLITE_FTS5_COMPILE_OPTION_SQL).fetchone()[0])

    driver_name = SQLITE_AIOSQLITE_DRIVER_NAME
    features = SqliteClient.features.replace(
        supports_positional_rows=True,
        max_bind_parameters=read_max_bind_parameters(),
        cascade_depth_limit=read_cascade_depth_limit(),
        # The library this process links is the server of every SQLite connection.
        supports_unhex=sqlite3.sqlite_version_info >= SQLITE_UNHEX_SERVER_VERSION,
        supports_drop_column=sqlite3.sqlite_version_info >= SQLITE_DROP_COLUMN_SERVER_VERSION,
        supports_strict_tables=sqlite3.sqlite_version_info >= SQLITE_STRICT_SERVER_VERSION,
        supports_full_text_index=read_full_text_support(),
    )
    driver_errors = SqliteDriverErrors(
        operational=sqlite3.OperationalError,
        integrity=sqlite3.IntegrityError,
        interface=sqlite3.InterfaceError,
        statement=(sqlite3.ProgrammingError, sqlite3.DataError, sqlite3.InternalError, sqlite3.NotSupportedError),
        database=sqlite3.DatabaseError,
        closed_connection_message=AIOSQLITE_NO_ACTIVE_CONNECTION_MESSAGE,
    )
    library_version: ClassVar[str] = sqlite3.sqlite_version
    has_automatic_index_collation_fault: ClassVar[bool] = (
        SQLITE_AUTOMATIC_INDEX_COLLATION_FAULT_SERVER_VERSIONS[0]
        <= sqlite3.sqlite_version_info
        < SQLITE_AUTOMATIC_INDEX_COLLATION_FAULT_SERVER_VERSIONS[1]
    )
    has_returning_foreign_key_error_fault: ClassVar[bool] = (
        SQLITE_RETURNING_FOREIGN_KEY_ERROR_FAULT_SERVER_VERSIONS[0]
        <= sqlite3.sqlite_version_info
        < SQLITE_RETURNING_FOREIGN_KEY_ERROR_FAULT_SERVER_VERSIONS[1]
    )

    @staticmethod
    def install_parameter_adapters() -> None:
        """Registers the text SQLite stores for Python values it has no native type for -
        process-wide in ``sqlite3``, so raw queries, arithmetic and annotations, where the field a
        value belongs to isn't known, get it too."""
        sqlite3.register_adapter(Decimal, SqliteParameterAdapters.adapt_decimal)
        sqlite3.register_adapter(datetime.date, lambda value: value.isoformat())
        sqlite3.register_adapter(datetime.datetime, SqliteParameterAdapters.adapt_datetime)
        sqlite3.register_adapter(datetime.time, SqliteParameterAdapters.adapt_time)

    async def swap_busy_timeout(self, connection: aiosqlite.Connection, milliseconds: int) -> int:
        async with connection.execute(SQLITE_BUSY_TIMEOUT_SQL) as cursor:
            row = await cursor.fetchone()
        await connection.execute(SQLITE_SET_BUSY_TIMEOUT_SQL.format(milliseconds=milliseconds))
        return int(row[0]) if row is not None else 0

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
            await SqliteFullTextQuery.install(self._connection)
            if self.load_sqlite_vec:
                await SqliteVectorExtension.install(self._connection)
            if self.spatial_extension is not None:
                spatial_reference_ids = await self.spatial_extension.install(self._connection)
                self.features = self.features.replace(spatial_reference_ids=spatial_reference_ids)

    async def create_connection(self, with_db: bool) -> None:
        if not self._connection:  # pragma: no branch
            start_time = time.perf_counter()
            connection = aiosqlite.connect(self.filename, isolation_level=None)
            # aiosqlite runs each connection on a thread of its own, started by the await below. A
            # daemon thread lets the interpreter exit when a program ends - on an exception, say -
            # without closing its connections; SQLite's journal rolls an unfinished transaction back.
            connection._thread.daemon = True
            try:
                opened_connection = self._connection = await connection
            except (sqlite3.Error, OSError) as error:
                ConnectFailureReports.record(self, error)
                raise DBConnectionError(
                    SQLITE_OPEN_FAILED_MESSAGE.format(filename=self.filename, error=error)
                ) from error
            try:
                await self._set_up_connection(opened_connection)
            except BaseException:
                # A connection whose setup failed - a PRAGMA, an extension that didn't load - isn't
                # kept open.
                await opened_connection.close()
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
        """Configures a just-opened connection: rows, double-quoted names, the PRAGMAs, then the
        functions and extensions ``_post_connect()`` installs.

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
        await self._post_connect()

    def acquire_connection(self) -> ConnectionWrapper[aiosqlite.Connection]:
        return AiosqliteConnectionWrapper(self._lock, self)

    def _get_transaction_client(self) -> AiosqliteTransactionClient:
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_transaction_client import (
            AiosqliteTransactionClient,
        )

        return AiosqliteTransactionClient(self)

    @SqliteClient.translate_exceptions
    async def execute_many(self, query: str, values: list[list[Any]]) -> None:
        async with self.acquire_connection() as connection:
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug("%s: %s", query, values)
            if self.binds_parameters_by_number and SQLITE_FIRST_NUMBERED_PLACEHOLDER in query:
                values = [self.get_values_by_number(row) for row in values]  # type: ignore[misc]
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
        values: list[Any] | None,
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

    @SqliteClient.translate_exceptions
    async def execute(
        self,
        query: str,
        values: list[Any] | None = None,
        *,
        returns_rows: bool | None = None,
        rows_by_position: bool = False,
    ) -> StatementResult:
        if SQLITE_NULL_BYTE in query:
            query = self._escape_null_bytes(query)
        async with self.acquire_connection() as connection:
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug("%s: %s", query, values)
            if values and self.binds_parameters_by_number and SQLITE_FIRST_NUMBERED_PLACEHOLDER in query:
                values = self.get_values_by_number(values)  # type: ignore[assignment]
            # One worker-thread hop for execute + fetch + row count, like execute_fetchall(). Rows
            # read by position, and the no rows of a write, are plain tuples - no sqlite3.Row built
            # per row; sqlite3.Row reads both by position and by name
            # (Features.supports_positional_rows).
            rows, row_count, inserted_id, description = await self._run_on_worker_thread(
                connection,
                self._fetch_rows_and_row_count,
                query,
                values,
                None if rows_by_position or returns_rows is False else sqlite3.Row,
            )
            return StatementResult(row_count, rows, inserted_id, description)

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

    @SqliteClient.translate_exceptions
    async def execute_described(self, query: str, values: list[Any] | None = None) -> DescribedResult:
        if SQLITE_NULL_BYTE in query:
            query = self._escape_null_bytes(query)
        async with self.acquire_connection() as connection:
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug("%s: %s", query, values)
            if values and self.binds_parameters_by_number and SQLITE_FIRST_NUMBERED_PLACEHOLDER in query:
                values = self.get_values_by_number(values)  # type: ignore[assignment]
            columns, rows, row_count = await self._run_on_worker_thread(
                connection, self._fetch_described_rows, query, values
            )
            return DescribedResult(columns=columns, rows=tuple(tuple(row) for row in rows), row_count=row_count)

    @SqliteClient.translate_exceptions
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


# sqlite3 adapts parameters process-wide - raw queries and expressions too, where the field isn't known.
AiosqliteClient.install_parameter_adapters()
