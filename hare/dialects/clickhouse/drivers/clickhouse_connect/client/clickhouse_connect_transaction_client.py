from __future__ import annotations

import logging
import uuid
from typing import Any

from clickhouse_connect.driver.asyncclient import AsyncClient

from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient
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
        await self._connection.command(sql)

    @ClickhouseClient.translate_exceptions
    async def copy(
        self, table: str, columns: list[str], records: list[tuple[Any, ...]], column_types: list[str]
    ) -> None:
        """Loads the records in one binary insert of the transaction - the table's column types read
        first through the connection the transaction was opened on: the library would read them in the
        transaction, which takes no query of the server's own tables."""
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("INSERT %s(%s): %d record(s)", table, columns, len(records))
        rows = await self._parent.execute_dicts(CLICKHOUSE_TABLE_COLUMN_TYPES_SQL, [table])
        types_by_column = {str(row["name"]): str(row["type"]) for row in rows}
        inserted_columns = self._get_inserted_columns(records)
        async with self.acquire_connection() as connection:
            await connection.insert(
                self.dialect.literals.quote_identifier(table),
                inserted_columns,
                column_names=columns,
                column_type_names=[types_by_column[column] for column in columns],
                column_oriented=True,
            )
