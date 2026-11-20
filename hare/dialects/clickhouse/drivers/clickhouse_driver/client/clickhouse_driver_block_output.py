from __future__ import annotations

from typing import Any

from clickhouse_driver import defines
from clickhouse_driver.columns.exceptions import ColumnTypeMismatchException, StructPackException
from clickhouse_driver.errors import TypeMismatchError
from clickhouse_driver.varint import write_varint
from clickhouse_driver.writer import write_binary_str, write_binary_uint8

from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_column_types import (
    ClickhouseDriverColumnTypes,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import CLICKHOUSE_DRIVER_COLUMN_WRITE_ERROR


class ClickhouseDriverBlockOutput:
    """Write native block columns with local codecs and the library's compression stream."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream

    def write(self, block: Any) -> None:
        """Write one block and delegate compression, checksums and flushing to the stream."""
        context = self.stream.context
        target = self.stream.fout
        revision = context.server_info.used_revision
        if revision >= defines.DBMS_MIN_REVISION_WITH_BLOCK_INFO:
            block.info.write(target)
        write_varint(block.num_columns, target)
        write_varint(block.num_rows, target)
        for index, (name, specification) in enumerate(block.columns_with_types):
            write_binary_str(name, target)
            write_binary_str(specification, target)
            if revision >= defines.DBMS_MIN_REVISION_WITH_CUSTOM_SERIALIZATION:
                write_binary_uint8(0, target)
            column = ClickhouseDriverColumnTypes.get_column_by_specification(
                specification, {"context": context, "types_check": block.types_check}
            )
            values = block.get_column_by_index(index)
            try:
                column.write_state_prefix(target, values)
                column.write_data(values, target)
            except (ColumnTypeMismatchException, StructPackException, OverflowError) as error:
                raise TypeMismatchError(
                    CLICKHOUSE_DRIVER_COLUMN_WRITE_ERROR.format(name=name, type=specification, error=error)
                ) from error
        self.stream.finalize()
