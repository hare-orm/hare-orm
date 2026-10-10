from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncGenerator
from typing import Any

from clickhouse_driver import Client
from clickhouse_driver.errors import (
    Error as ClickhouseDriverError,
    NetworkError,
    ServerException,
    SocketTimeoutError,
)

from hare.dialects.base.results.described_result import DescribedResult
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient
from hare.dialects.clickhouse.client.clickhouse_external_sets import ClickhouseExternalSets
from hare.dialects.clickhouse.client.constants import CLICKHOUSE_BEGIN_TRANSACTION_SQL, CLICKHOUSE_ROLLBACK_SQL
from hare.dialects.clickhouse.client.declarations import ClickhouseDriverErrors, ClickhouseExternalSet
from hare.dialects.clickhouse.constants import CLICKHOUSE_DEFAULT_NATIVE_PORT
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_client_info import (
    ClickhouseDriverClientInfo,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_column_types import (
    ClickhouseDriverColumnTypes,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_connections import (
    ClickhouseDriverConnections,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_decompressors import (
    ClickhouseDriverDecompressors,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_map_columns import (
    ClickhouseDriverMapColumns,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_moment_columns import (
    ClickhouseDriverMomentColumns,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_row_stream import (
    ClickhouseDriverRowStream,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_settings import (
    ClickhouseDriverSettings,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_tuple_columns import (
    ClickhouseDriverTupleColumns,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import (
    CLICKHOUSE_DRIVER_COMPRESSION,
    CLICKHOUSE_DRIVER_CONNECTION_OPTION_DEFAULTS,
    CLICKHOUSE_DRIVER_CONNECTION_OPTIONS,
    CLICKHOUSE_DRIVER_INSERT_STATEMENT_TEMPLATE,
)
from hare.dialects.clickhouse.drivers.constants import (
    CLICKHOUSE_DRIVER_DRIVER_NAME,
    CLICKHOUSE_UNKNOWN_DATABASE_ERROR_CODES,
)
from hare.instrumentation.pools.pool_registry import PoolRegistry


class ClickhouseDriverClient(ClickhouseClient):
    """A ClickHouse connection through the clickhouse-driver library - the server's native protocol
    over TCP. The library is synchronous: a statement runs on a worker thread keeping a connection
    of its own, and the event loop waits for it."""

    driver_name = CLICKHOUSE_DRIVER_DRIVER_NAME
    default_port = CLICKHOUSE_DEFAULT_NATIVE_PORT
    connection_options = CLICKHOUSE_DRIVER_CONNECTION_OPTIONS
    connection_option_defaults = CLICKHOUSE_DRIVER_CONNECTION_OPTION_DEFAULTS
    # The rows of execute(rows_by_position=True) are the library's own tuples. execute_many() sends
    # a statement per row - a write of many rows that copy() can't load is one multi-row statement.
    # The library reads no path of a JSON column holding a Dynamic value.
    reads_json_type = False
    features = ClickhouseClient.features.replace(
        supports_positional_rows=True, execute_many_scales_poorly=True, supports_pool_status=True
    )

    # A socket that breaks under a running statement raises Python's own errors, unwrapped - the
    # end of its data among them.
    driver_errors = ClickhouseDriverErrors(
        server=ServerException,
        connection=(NetworkError, SocketTimeoutError, ConnectionError, TimeoutError, EOFError),
        driver=(ClickhouseDriverError, OSError, EOFError),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._server_version: tuple[int, ...] | None = None

    async def create_connection(self, with_db: bool) -> None:
        ClickhouseDriverMomentColumns.install()
        ClickhouseDriverMapColumns.install()
        ClickhouseDriverColumnTypes.install()
        ClickhouseDriverTupleColumns.install()
        ClickhouseDriverClientInfo.install()
        ClickhouseDriverDecompressors.install()
        ClickhouseDriverSettings.install()
        connection_settings = self.get_connection_settings(with_db)
        connections = ClickhouseDriverConnections(self.options["max_size"], connection_settings, self.pool_statistics)
        try:
            self._server_version = await connections.run(self._read_server_version)
        except BaseException:
            await connections.close()
            raise
        self._connection = connections
        await self._post_connect()

    def get_connection_settings(self, with_db: bool) -> dict[str, Any]:
        """The settings the library's connections are opened with.

        Args:
            with_db: Whether they use the connection's database.

        Returns:
            The settings.
        """
        connection_settings: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "secure": self.options["secure"],
            "compression": CLICKHOUSE_DRIVER_COMPRESSION if self.options["compress"] else False,
            "connect_timeout": self.options["connect_timeout"],
            "send_receive_timeout": self.options["send_receive_timeout"],
            # A setting the server doesn't have is not marked important: the server skips it.
            "settings": {**self.get_session_settings(), **self.get_optional_session_settings()},
        }
        if with_db and self.database:
            connection_settings["database"] = self.database
        return connection_settings

    async def check_transactions(self) -> None:
        connections: ClickhouseDriverConnections = self._connection
        await connections.run(self._begin_and_roll_back)

    @staticmethod
    def _begin_and_roll_back(library_client: Client) -> None:
        library_client.execute(CLICKHOUSE_BEGIN_TRANSACTION_SQL)
        library_client.execute(CLICKHOUSE_ROLLBACK_SQL)

    def _get_transaction_client(self) -> Any:
        if not self.features.supports_transactions:
            return super()._get_transaction_client()
        # Local import: the transaction client's module imports this one.
        from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_transaction_client import (
            ClickhouseDriverTransactionClient,
        )

        return ClickhouseDriverTransactionClient(self)

    @staticmethod
    def _read_server_version(library_client: Client) -> tuple[int, ...]:
        return tuple(library_client.connection.server_info.version_tuple())

    async def get_server_version(self) -> tuple[int, ...] | None:
        return self._server_version

    def get_native_port(self) -> int | None:
        return self.options["native_port"] or self.port

    def get_pool_occupancy(self) -> tuple[int, int, int, int, int] | None:
        connections: ClickhouseDriverConnections | None = self._connection
        return None if connections is None else connections.get_occupancy()

    async def close(self) -> None:
        connections, self._connection = self._connection, None
        if connections is not None:
            PoolRegistry.remove(self)
            await connections.close()

    async def db_create(self) -> None:
        await self.create_connection(with_db=False)
        try:
            await self._connection.run(
                self._run_command,
                self.get_database_statement(
                    f"CREATE DATABASE IF NOT EXISTS {self.dialect.literals.quote_identifier(self.database or '')}"
                ),
            )
        finally:
            await self.close()

    async def db_delete(self) -> None:
        await self.close()
        await self.create_connection(with_db=False)
        try:
            await self._connection.run(
                self._run_command,
                self.get_database_statement(
                    f"DROP DATABASE IF EXISTS {self.dialect.literals.quote_identifier(self.database or '')}"
                ),
            )
        except ServerException as error:
            if self.get_error_code(error) not in CLICKHOUSE_UNKNOWN_DATABASE_ERROR_CODES:
                raise
        finally:
            await self.close()

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
            async with self.acquire_connection() as connections:
                return await connections.run(self._read_rows, sql, rows_by_position, external_sets)
        sql = self.get_inlined_query(query, values)
        # Built first: a cluster's statements may read the database.
        written_statements = await self.get_written_statements(sql)
        async with self.acquire_connection() as connections:
            if written_statements is not None:
                return await connections.run(self._count_rows_then_write, written_statements[1], written_statements[0])
            if returns_rows:
                return await connections.run(self._read_rows, sql, rows_by_position)
            return await connections.run(self._run_command, sql)

    @staticmethod
    def _read_rows(
        library_client: Client,
        sql: str,
        rows_by_position: bool,
        external_sets: list[ClickhouseExternalSet] | None = None,
    ) -> StatementResult:
        external_tables = None
        if external_sets:
            external_tables = []
            for external_set in external_sets:
                # The rows as tuples in the structure's column order - the library's own form, which it
                # reads without turning a mapping of each row back into one.
                external_tables.append(
                    {
                        "name": external_set.name,
                        "structure": ClickhouseExternalSets.get_structure(external_set.value_set),
                        "data": ClickhouseExternalSets.get_rows(external_set.value_set),
                    }
                )
        position_rows, columns = library_client.execute(sql, with_column_types=True, external_tables=external_tables)
        if rows_by_position:
            # The rows as read - no mapping built per row; the names in the description.
            return StatementResult(len(position_rows), position_rows, description=tuple(columns))
        column_names = [column[0] for column in columns]
        rows = [dict(zip(column_names, row, strict=True)) for row in position_rows]
        return StatementResult(len(rows), rows)

    @staticmethod
    def _run_command(library_client: Client, sql: str) -> StatementResult:
        library_client.execute(sql)
        return StatementResult(library_client.last_query.progress.written_rows, [])

    @staticmethod
    def _count_rows_then_write(library_client: Client, count_sql: str, write_sql: str) -> StatementResult:
        # The rows the write matches, counted before it changes them.
        row_count = library_client.execute(count_sql)[0][0]
        library_client.execute(write_sql)
        return StatementResult(row_count, [])

    async def read_batches(self, sql: str, batch_size: int) -> AsyncGenerator[list[Any]]:
        async with (
            self.acquire_connection() as connections,
            contextlib.aclosing(ClickhouseDriverRowStream(connections, sql, batch_size).read_batches()) as batches,
        ):
            async for batch in batches:
                yield batch

    @staticmethod
    def _run_commands(library_client: Client, statements: list[str]) -> None:
        for statement in statements:
            library_client.execute(statement)

    @ClickhouseClient.translate_exceptions
    async def execute_described(self, query: str, values: list[Any] | None = None) -> DescribedResult:
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("%s: %s", query, values)
        if not self.returns_rows(query):
            sql = self.get_inlined_query(query, values)
            async with self.acquire_connection() as connections:
                written = await connections.run(self._run_command, sql)
            return DescribedResult(columns=(), rows=(), row_count=written.row_count)
        sql, external_sets = self.get_read_query(query, values)
        async with self.acquire_connection() as connections:
            result = await connections.run(self._read_rows, sql, True, external_sets)
            return DescribedResult(columns=result.column_names, rows=tuple(result.rows), row_count=result.row_count)

    @ClickhouseClient.translate_exceptions
    async def execute_many(self, query: str, values: list[list[Any]]) -> None:
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("%s: %s", query, values)
        async with self.acquire_connection() as connections:
            await connections.run(
                self._run_commands, [self.get_inlined_query(query, row_values) for row_values in values]
            )

    @ClickhouseClient.translate_exceptions
    async def copy(
        self, table: str, columns: list[str], records: list[tuple[Any, ...]], column_types: list[str]
    ) -> None:
        """Loads ``records`` into ``table`` in one binary insert - blocks of ClickHouse's native
        protocol: no SQL text is built from the values and the server parses none. ``column_types``
        is unused: the server names the table's column types to the library itself.
        """
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("INSERT %s(%s): %d record(s)", table, columns, len(records))
        quote_identifier = self.dialect.literals.quote_identifier
        statement = CLICKHOUSE_DRIVER_INSERT_STATEMENT_TEMPLATE.format(
            table=quote_identifier(table), columns=", ".join(map(quote_identifier, columns))
        )
        async with self.acquire_connection() as connections:
            await connections.run(self._insert_records, statement, records)

    @classmethod
    def _insert_records(cls, library_client: Client, statement: str, records: list[tuple[Any, ...]]) -> None:
        library_client.execute(statement, cls._get_inserted_columns(records), columnar=True)

    @ClickhouseClient.translate_exceptions
    async def execute_script(self, query: str) -> None:
        self.log.debug(query)
        statements = await self.get_script_statements(query)
        async with self.acquire_connection() as connections:
            await connections.run(self._run_commands, statements)
