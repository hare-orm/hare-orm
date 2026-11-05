from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.insert import InsertContext

from hare.dialects.clickhouse.client.clickhouse_transaction_client import ClickhouseTransactionClient
from hare.dialects.clickhouse.client.constants import CLICKHOUSE_TABLE_COLUMN_TYPES_SQL
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_client import (
    ClickhouseConnectClient,
)


class ClickhouseConnectTransactionClient(ClickhouseTransactionClient, ClickhouseConnectClient):
    """A transaction of a clickhouse-connect connection - an HTTP session of its own, which the
    server keeps the transaction in between the requests."""

    _parent: ClickhouseConnectClient

    async def open_transaction_connection(self) -> AsyncClient:
        return await self.open_library_client(with_db=True, session_id=str(uuid.uuid4()))

    async def close_transaction_connection(self, connection: AsyncClient) -> None:
        await connection.close()

    async def send_transaction_statement(self, sql: str) -> None:
        await self.send_command(self._connection, sql)

    @asynccontextmanager
    async def insert_context(
        self, connection: AsyncClient, table: str, columns: list[str], schema: str | None
    ) -> AsyncGenerator[InsertContext]:
        """Create an empty context using types read through the parent, without the shared cache.

        A transaction cannot query the server's system tables. Supplying type names prevents
        the library from describing the table inside its session; the inherited COPY prepares
        codecs and moment ticks before assigning data and sending the insert.
        """
        rows = await self._parent.execute_dicts(CLICKHOUSE_TABLE_COLUMN_TYPES_SQL, [schema or "", table])
        types_by_column = {str(row["name"]): str(row["type"]) for row in rows}
        yield await connection.create_insert_context(
            self.dialect.literals.qualify_table_name(table, schema),
            columns,
            column_type_names=[types_by_column[column] for column in columns],
            column_oriented=True,
        )
