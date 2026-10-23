from __future__ import annotations

from typing import Any

from clickhouse_driver import defines
from clickhouse_driver.block import BlockInfo, ColumnOrientedBlock
from clickhouse_driver.columns.service import read_column
from clickhouse_driver.reader import read_binary_str, read_binary_uint8
from clickhouse_driver.streams.native import BlockInputStream
from clickhouse_driver.varint import read_varint

from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import (
    CLICKHOUSE_DRIVER_FIXED_VALUE_WIDTH_PREFIXES,
    CLICKHOUSE_DRIVER_FIXED_VALUE_WIDTHS,
    CLICKHOUSE_DRIVER_PROFILE_EVENTS_FIRST_COLUMN,
    CLICKHOUSE_DRIVER_STRING_TYPE,
)


class ClickhouseDriverServiceBlockStream(BlockInputStream):  # type: ignore[misc]  # the library ships no types
    """The stream of the uncompressed blocks the server sends besides a statement's rows - its log
    lines and its ProfileEvents. The library reads a ProfileEvents block whole, a moment and a time
    zone conversion per row, and drops it; its values are passed over unread here, the block given
    without rows. A log block is read as the library reads it."""

    def read(self, use_numpy: bool | None = None) -> Any:
        info = BlockInfo()
        revision = self.context.server_info.used_revision
        if revision >= defines.DBMS_MIN_REVISION_WITH_BLOCK_INFO:
            info.read(self.fin)
        column_count = read_varint(self.fin)
        row_count = read_varint(self.fin)
        names: list[str] = []
        types: list[str] = []
        data: list[Any] = []
        is_profile_events = False
        for index in range(column_count):
            column_name = read_binary_str(self.fin)
            column_type = read_binary_str(self.fin)
            names.append(column_name)
            types.append(column_type)
            if index == 0:
                is_profile_events = column_name == CLICKHOUSE_DRIVER_PROFILE_EVENTS_FIRST_COLUMN
            has_custom_serialization = False
            if revision >= defines.DBMS_MIN_REVISION_WITH_CUSTOM_SERIALIZATION:
                has_custom_serialization = bool(read_binary_uint8(self.fin))
            if not row_count:
                continue
            if is_profile_events and not has_custom_serialization and self._skip_values(column_type, row_count):
                continue
            column = read_column(
                self.context,
                column_type,
                row_count,
                self.fin,
                use_numpy=use_numpy,
                has_custom_serialization=has_custom_serialization,
            )
            if not is_profile_events:
                data.append(column)
        if is_profile_events:
            # Read past - the library drops the block.
            return ColumnOrientedBlock(columns_with_types=list(zip(names, types, strict=True)), data=[], info=info)
        if self.context.client_settings["use_numpy"]:
            from clickhouse_driver.numpy.block import NumpyColumnOrientedBlock

            return NumpyColumnOrientedBlock(
                columns_with_types=list(zip(names, types, strict=True)), data=data, info=info
            )
        return ColumnOrientedBlock(columns_with_types=list(zip(names, types, strict=True)), data=data, info=info)

    def _skip_values(self, column_type: str, row_count: int) -> bool:
        """Reads past the values of a column without converting them.

        Args:
            column_type: The column's type.
            row_count: The block's rows.

        Returns:
            Whether the values were read past - False for a type of no known layout, read by the
            library then.
        """
        if column_type == CLICKHOUSE_DRIVER_STRING_TYPE:
            self.fin.read_strings(row_count)
            return True
        width = CLICKHOUSE_DRIVER_FIXED_VALUE_WIDTHS.get(column_type)
        if width is None:
            for prefix, prefix_width in CLICKHOUSE_DRIVER_FIXED_VALUE_WIDTH_PREFIXES:
                if column_type.startswith(prefix):
                    width = prefix_width
                    break
            else:
                return False
        self.fin.read(width * row_count)
        return True
