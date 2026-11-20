from __future__ import annotations

from typing import Any

from clickhouse_driver import defines
from clickhouse_driver.block import BlockInfo, ColumnOrientedBlock
from clickhouse_driver.reader import read_binary_str, read_binary_uint8
from clickhouse_driver.varint import read_varint

from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_column_types import (
    ClickhouseDriverColumnTypes,
)


class ClickhouseDriverBlockInput:
    """Read native blocks through a local column factory and the library's byte stream.

    The stream retains socket, decompression and checksum handling. This adapter covers the
    block header and column-factory boundary of clickhouse-driver 0.2.11 and later.
    """

    def __init__(self, stream: Any) -> None:
        self.stream = stream

    def read(self, use_numpy: bool | None = None) -> Any:
        """Read one block, including its metadata and each column's serialization prefix."""
        context = self.stream.context
        source = self.stream.fin
        revision = context.server_info.used_revision
        info = BlockInfo()
        if revision >= defines.DBMS_MIN_REVISION_WITH_BLOCK_INFO:
            info.read(source)
        column_count, row_count = read_varint(source), read_varint(source)
        columns = []
        data = []
        for _ in range(column_count):
            name, specification = read_binary_str(source), read_binary_str(source)
            columns.append((name, specification))
            custom_serialization = (
                bool(read_binary_uint8(source))
                if revision >= defines.DBMS_MIN_REVISION_WITH_CUSTOM_SERIALIZATION
                else False
            )
            if row_count:
                column = ClickhouseDriverColumnTypes.get_column_by_specification(
                    specification, {"context": context, "has_custom_serialization": custom_serialization}, use_numpy
                )
                column.read_state_prefix(source)
                data.append(column.read_data(row_count, source))
        return ColumnOrientedBlock(columns_with_types=columns, data=data, info=info)
