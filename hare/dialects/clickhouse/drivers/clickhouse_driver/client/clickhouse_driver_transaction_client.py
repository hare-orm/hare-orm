from __future__ import annotations

from hare.dialects.clickhouse.client.clickhouse_transaction_client import ClickhouseTransactionClient
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_client import ClickhouseDriverClient
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_connections import (
    ClickhouseDriverConnections,
)


class ClickhouseDriverTransactionClient(ClickhouseTransactionClient, ClickhouseDriverClient):
    """A transaction of a clickhouse-driver connection - a TCP connection of its own, on one worker
    thread, which the server keeps the transaction on."""

    _parent: ClickhouseDriverClient

    async def open_transaction_connection(self) -> ClickhouseDriverConnections:
        return ClickhouseDriverConnections(1, self.get_connection_settings(with_db=True), self.pool_statistics)

    async def close_transaction_connection(self, connection: ClickhouseDriverConnections) -> None:
        await connection.close()

    async def send_transaction_statement(self, sql: str) -> None:
        await self._connection.run(self._run_command, sql)
