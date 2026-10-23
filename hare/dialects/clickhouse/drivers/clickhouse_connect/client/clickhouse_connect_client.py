from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator, Sequence
from typing import Any, ClassVar

import clickhouse_connect
from aiohttp import ClientPayloadError
from clickhouse_connect.datatypes.dynamic import typed_variant
from clickhouse_connect.datatypes.temporal import DateTime64
from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.exceptions import (
    ClickHouseError,
    DatabaseError,
    OperationalError as ClickHouseOperationalError,
)
from clickhouse_connect.driver.external import ExternalData

from hare.core.caching.cache import Cache
from hare.dialects.base.results.described_result import DescribedResult
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient
from hare.dialects.clickhouse.client.clickhouse_external_sets import ClickhouseExternalSets
from hare.dialects.clickhouse.client.clickhouse_streamed_row import ClickhouseStreamedRow
from hare.dialects.clickhouse.client.constants import (
    CLICKHOUSE_BEGIN_TRANSACTION_SQL,
    CLICKHOUSE_HTTP_RECEIVE_TIMEOUT_MARGIN_SECONDS,
    CLICKHOUSE_ROLLBACK_SQL,
    CLICKHOUSE_TAB_SEPARATED_FORMAT,
    CLICKHOUSE_WITH_TOTALS_PATTERN,
)
from hare.dialects.clickhouse.client.declarations import ClickhouseDriverErrors, ClickhouseExternalSet
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_dynamic_type import (
    ClickhouseConnectDynamicType,
)
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_totals_query import (
    ClickhouseConnectTotalsQuery,
)
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_whole_response_query import (
    ClickhouseConnectWholeResponseQuery,
)
from hare.dialects.clickhouse.drivers.clickhouse_connect.constants import (
    CLICKHOUSE_CONNECT_COMMAND_SETTINGS,
    CLICKHOUSE_CONNECT_INSERTED_COLUMN_TYPES_CACHE_SIZE,
    CLICKHOUSE_CONNECT_MOMENT_COLUMN_TYPES,
    CLICKHOUSE_CONNECT_SCHEMA_CHANGE_PATTERN,
    CLICKHOUSE_CONNECT_UNREADABLE_SERVER_ERROR_MARK,
)
from hare.dialects.clickhouse.drivers.constants import (
    CLICKHOUSE_CONNECT_DRIVER_NAME,
    CLICKHOUSE_MOMENT_EPOCH,
)
from hare.native.native_modules import NativeModules


class ClickhouseConnectClient(ClickhouseClient):
    """A ClickHouse connection through clickhouse-connect's asyncio HTTP client."""

    driver_name = CLICKHOUSE_CONNECT_DRIVER_NAME
    # The rows of execute(rows_by_position=True) are clickhouse-connect's own tuples. execute_many()
    # sends an HTTP request per row - a write of many rows that copy() can't load is one multi-row
    # statement.
    features = ClickhouseClient.features.replace(supports_positional_rows=True, execute_many_scales_poorly=True)

    # A response the server breaks off reaches the library as the HTTP library's own error.
    driver_errors = ClickhouseDriverErrors(
        server=DatabaseError, connection=ClickHouseOperationalError, driver=(ClickHouseError, ClientPayloadError)
    )
    #: ``rust.native.rows`` - converts a column's moments to ticks; None where it isn't built.
    native_rows: ClassVar[Any] = NativeModules.rows
    #: (host, port, database, table, columns) -> the library's type of each column, as the server named
    #: it before the first insert - the library asks the server before every insert. Emptied by every
    #: change of a schema made through any connection.
    inserted_column_types: ClassVar[Cache[list[Any]]] = Cache(
        CLICKHOUSE_CONNECT_INSERTED_COLUMN_TYPES_CACHE_SIZE, holds_sql=False, keyed_by_model=False
    )

    def get_hare_error(self, error: Exception, sql: Any, parameters: Any) -> Exception:
        # The library reports a server error it couldn't read out of the stream as a failed
        # connection - the connection is fine, the statement failed.
        if CLICKHOUSE_CONNECT_UNREADABLE_SERVER_ERROR_MARK in str(error):
            return self._get_operational_error(error, sql=sql, parameters=parameters)
        return super().get_hare_error(error, sql, parameters)

    async def create_connection(self, with_db: bool) -> None:
        self._connection = await self.open_library_client(with_db)
        await self._post_connect()

    async def open_library_client(self, with_db: bool, session_id: str | None = None) -> AsyncClient:
        """Opens a library client with the connection's settings.

        Args:
            with_db: Whether it uses the connection's database.
            session_id: The server session its requests run in - one of a transaction; None for none.

        Returns:
            The client.
        """
        ClickhouseConnectDynamicType.install()
        library_client = await clickhouse_connect.get_async_client(
            session_id=session_id,
            host=self.host,
            port=self.port,
            username=self.user,
            password=self.password,
            database=self.database if with_db else None,
            secure=self.options["secure"],
            compress=self.options["compress"],
            connect_timeout=self.options["connect_timeout"],
            # The server stops a statement at send_receive_timeout (max_execution_time) - its error
            # arrives before the client would stop waiting.
            send_receive_timeout=self.options["send_receive_timeout"] + CLICKHOUSE_HTTP_RECEIVE_TIMEOUT_MARGIN_SECONDS,
            settings=self.get_session_settings(),
        )
        return library_client

    async def check_transactions(self) -> None:
        # In no database: the check runs before the connection's database is created too.
        library_client = await self.open_library_client(with_db=False, session_id=str(uuid.uuid4()))
        try:
            await self.send_command(library_client, CLICKHOUSE_BEGIN_TRANSACTION_SQL)
            await self.send_command(library_client, CLICKHOUSE_ROLLBACK_SQL)
        finally:
            await library_client.close()

    def _get_transaction_client(self) -> Any:
        if not self.features.supports_transactions:
            return super()._get_transaction_client()
        # Local import: the transaction client's module imports this one.
        from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_transaction_client import (
            ClickhouseConnectTransactionClient,
        )

        return ClickhouseConnectTransactionClient(self)

    async def get_server_version(self) -> tuple[int, ...] | None:
        version = str(self._connection.server_version)
        return tuple(int(part) for part in version.split(".") if part.isdigit())

    async def close(self) -> None:
        self.closes_started += 1
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()

    @staticmethod
    async def send_command(library_client: AsyncClient, sql: str) -> Any:
        """Sends a statement returning no rows - answered once it is done, its error read whole.

        Args:
            library_client: The library client.
            sql: The statement.

        Returns:
            The library's summary of it.
        """
        return await library_client.command(sql, settings=CLICKHOUSE_CONNECT_COMMAND_SETTINGS)

    async def run_command_without_database(self, statement: str) -> None:
        await self.send_command(self._connection, statement)

    @ClickhouseClient.translate_exceptions
    async def execute(
        self,
        query: str,
        values: list[Any] | None = None,
        *,
        returns_rows: bool | None = None,
        rows_by_position: bool = False,
    ) -> StatementResult:
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("%s: %s", query, values)
        if returns_rows if returns_rows is not None else self.returns_rows(query):
            sql, external_sets = self.get_read_query(query, values)
            async with self.acquire_connection() as connection:
                if CLICKHOUSE_WITH_TOTALS_PATTERN.search(sql):
                    return await ClickhouseConnectTotalsQuery.run(
                        connection, sql, self.get_external_data(external_sets), rows_by_position
                    )
                result = await ClickhouseConnectWholeResponseQuery.run(
                    connection, sql, self.get_external_data(external_sets)
                )
            column_names = result.column_names
            if rows_by_position:
                # The rows as read - no mapping built per row; the names in the description.
                position_rows = result.result_rows
                return StatementResult(
                    len(position_rows),
                    position_rows,
                    description=tuple(zip(column_names, result.column_types, strict=True)),
                )
            rows = [dict(zip(column_names, row, strict=True)) for row in result.result_rows]
            return StatementResult(len(rows), rows)
        sql = self.get_inlined_query(query, values)
        row_write_sql = await self.get_row_write_sql(sql)
        async with self.acquire_connection() as connection:
            if row_write_sql is not None:
                try:
                    await self.send_command(connection, row_write_sql)
                except self.driver_errors.driver as error:
                    await self.kill_failed_mutations(row_write_sql, error, connection.command)
                    raise
                return StatementResult(None, [])
            if returns_rows:
                result = await ClickhouseConnectWholeResponseQuery.run(connection, sql)
                column_names = result.column_names
                if rows_by_position:
                    # The rows as read - no mapping built per row; the names in the description.
                    position_rows = result.result_rows
                    return StatementResult(
                        len(position_rows),
                        position_rows,
                        description=tuple(zip(column_names, result.column_types, strict=True)),
                    )
                rows = [dict(zip(column_names, row, strict=True)) for row in result.result_rows]
                return StatementResult(len(rows), rows)
            try:
                summary = await self.send_command(connection, sql)
            except self.driver_errors.driver as error:
                await self.kill_failed_mutations(sql, error, connection.command)
                raise
            finally:
                self.forget_inserted_column_types(sql)
        written_rows = getattr(summary, "written_rows", None)
        return StatementResult(written_rows, [])

    @staticmethod
    def get_external_data(external_sets: list[ClickhouseExternalSet]) -> ExternalData | None:
        """The external tables of a read, as the library sends them - ``TabSeparated`` data.

        Args:
            external_sets: The tables.

        Returns:
            The library's external data; None for no table.
        """
        if not external_sets:
            return None
        external_data = ExternalData()
        for external_set in external_sets:
            external_data.add_file(
                file_name=external_set.name,
                data=ClickhouseExternalSets.get_tab_separated_data(external_set.value_set),
                fmt=CLICKHOUSE_TAB_SEPARATED_FORMAT,
                structure=", ".join(
                    f"{name} {column_type}"
                    for name, column_type in ClickhouseExternalSets.get_structure(external_set.value_set)
                ),
            )
        return external_data

    @ClickhouseClient.translate_exceptions
    async def execute_described(self, query: str, values: list[Any] | None = None) -> DescribedResult:
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("%s: %s", query, values)
        if not self.returns_rows(query):
            sql = self.get_inlined_query(query, values)
            async with self.acquire_connection() as connection:
                try:
                    summary = await self.send_command(connection, sql)
                finally:
                    self.forget_inserted_column_types(sql)
            return DescribedResult(columns=(), rows=(), row_count=getattr(summary, "written_rows", 0) or 0)
        sql, external_sets = self.get_read_query(query, values)
        async with self.acquire_connection() as connection:
            result = await ClickhouseConnectWholeResponseQuery.run(
                connection, sql, self.get_external_data(external_sets)
            )
        rows = tuple(tuple(row) for row in result.result_rows)
        return DescribedResult(columns=tuple(result.column_names), rows=rows, row_count=len(rows))

    @ClickhouseClient.translate_exceptions
    async def execute_many(self, query: str, values: list[list[Any]]) -> None:
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("%s: %s", query, values)
        async with self.acquire_connection() as connection:
            for row_values in values:
                await self.send_command(connection, self.get_inlined_query(query, row_values))

    @staticmethod
    def get_inserted_typed_value(value: Any, column_type: str) -> Any:
        # The library's own value of a type, which its Variant (and the replaced Dynamic) writes.
        return typed_variant(value, column_type)

    @ClickhouseClient.translate_exceptions
    async def copy(
        self,
        table: str,
        columns: list[str],
        records: list[tuple[Any, ...]],
        column_types: list[str],
        schema: str | None = None,
    ) -> None:
        """Loads ``records`` into ``table`` in one binary insert (ClickHouse's Native format) - no SQL
        text is built from the values and the server parses none. ``column_types`` is unused: the
        server names the table's column types to clickhouse-connect, once per table.
        """
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("INSERT %s(%s): %d record(s)", table, columns, len(records))
        inserted_columns = self._get_inserted_columns(records)
        key = (self.host, self.port, schema or self.database, table, tuple(columns))
        server_column_types: list[Any] | None = ClickhouseConnectClient.inserted_column_types.get(key)
        async with self.acquire_connection() as connection:
            # What the library's insert() does - asking the server for the column types only the
            # first time.
            context = await connection.create_insert_context(
                self.dialect.literals.qualify_table_name(table, schema),
                columns,
                column_types=server_column_types,
                column_oriented=True,
            )
            context.data = self._get_moment_tick_columns(inserted_columns, context.column_types)
            try:
                await connection.data_insert(context)
            except BaseException:
                # The table may have changed since the types were read - they are read again.
                if key in ClickhouseConnectClient.inserted_column_types:
                    del ClickhouseConnectClient.inserted_column_types[key]
                raise
        if server_column_types is None:
            ClickhouseConnectClient.inserted_column_types[key] = list(context.column_types)

    @staticmethod
    def forget_inserted_column_types(sql: str) -> None:
        """Has the column types of every table read again by its next insert when a statement may
        have changed a schema.

        Args:
            sql: The statement run.
        """
        if CLICKHOUSE_CONNECT_SCHEMA_CHANGE_PATTERN.match(sql):
            ClickhouseConnectClient.inserted_column_types.clear()

    @classmethod
    def _get_moment_tick_columns(
        cls, columns: list[Sequence[Any]], column_types: Sequence[Any]
    ) -> list[Sequence[Any]]:
        """The columns, the aware moments of each ``DateTime64`` column as the whole ticks the column
        stores - the library works them out one moment at a time, through a float.

        Args:
            columns: The inserted columns.
            column_types: The library's type of each column, as the server names it.

        Returns:
            The columns to insert.
        """
        if cls.native_rows is None:
            return columns
        tick_columns = list(columns)
        for position, (column, column_type) in enumerate(zip(columns, column_types, strict=True)):
            if (
                not isinstance(column_type, DateTime64)
                or not set(map(type, column)) <= CLICKHOUSE_CONNECT_MOMENT_COLUMN_TYPES
            ):
                continue
            try:
                tick_columns[position] = cls.native_rows.get_moment_ticks(
                    column, CLICKHOUSE_MOMENT_EPOCH, column_type.scale
                )
            except TypeError:
                # A naive moment - the library reads it in a time zone.
                continue
        return tick_columns

    async def read_batches(self, sql: str, batch_size: int) -> AsyncGenerator[list[Any]]:
        async with self.acquire_connection() as connection:
            stream = await connection.query_row_block_stream(sql, settings={"max_block_size": batch_size})
            # Leaving the stream closes its response - the server stops sending.
            async with stream:
                row_class = ClickhouseStreamedRow.get_row_class(tuple(stream.source.column_names))
                async for block in stream:
                    yield list(map(row_class, block))

    @ClickhouseClient.translate_exceptions
    async def execute_script(self, query: str) -> None:
        self.log.debug(query)
        statements = self.split_script(query)
        async with self.acquire_connection() as connection:
            try:
                for statement in statements:
                    try:
                        await self.send_command(connection, statement)
                    except self.driver_errors.driver as error:
                        await self.kill_failed_mutations(statement, error, connection.command)
                        raise
            finally:
                # A script changes schemas.
                ClickhouseConnectClient.inserted_column_types.clear()
