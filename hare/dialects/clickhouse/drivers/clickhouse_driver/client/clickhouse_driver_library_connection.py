from __future__ import annotations

from clickhouse_driver.connection import Connection

from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_service_block_stream import (
    ClickhouseDriverServiceBlockStream,
)


class ClickhouseDriverLibraryConnection(Connection):  # type: ignore[misc]  # the library ships no types
    """clickhouse-driver's connection, reading the blocks the server sends besides a statement's rows
    through ``ClickhouseDriverServiceBlockStream``."""

    def _init_connection(self, host: str, port: int) -> None:
        super()._init_connection(host, port)
        self.block_in_raw = ClickhouseDriverServiceBlockStream(self.fin, self.context)
