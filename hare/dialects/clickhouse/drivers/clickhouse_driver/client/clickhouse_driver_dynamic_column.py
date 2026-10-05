from __future__ import annotations

from typing import Any

from clickhouse_driver.columns.dynamiccolumn import DynamicColumn
from clickhouse_driver.varint import write_varint
from clickhouse_driver.writer import write_binary_uint64

from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import CLICKHOUSE_DRIVER_VARIANT_BASIC_MODE
from hare.dialects.clickhouse.drivers.clickhouse_shared_variant_values import ClickhouseSharedVariantValues
from hare.dialects.clickhouse.drivers.constants import (
    CLICKHOUSE_DYNAMIC_SHARED_DISCRIMINATOR,
    CLICKHOUSE_DYNAMIC_STRUCTURE_VERSION,
    CLICKHOUSE_DYNAMIC_WRITTEN_MAX_TYPES,
    CLICKHOUSE_VARIANT_NULL_DISCRIMINATOR,
)


class ClickhouseDriverDynamicColumn(DynamicColumn):  # type: ignore[misc]
    """clickhouse-driver's ``Dynamic`` column, read as the library reads one inside a ``JSON`` column -
    a value of the shared variant through ``ClickhouseSharedVariantValues`` - and written: every value
    in the shared variant, with its type.

    Args:
        column_by_specification_getter: Builds the library's column of a type.
        **column_options: The library's column options.
    """

    def __init__(self, column_by_specification_getter: Any, **column_options: Any) -> None:
        super().__init__(
            column_by_specification_getter, shared_value_decoder=ClickhouseSharedVariantValues, **column_options
        )

    def write_state_prefix(self, buf: Any, items: Any = None) -> None:
        write_binary_uint64(CLICKHOUSE_DYNAMIC_STRUCTURE_VERSION, buf)
        write_varint(CLICKHOUSE_DYNAMIC_WRITTEN_MAX_TYPES, buf)
        write_varint(0, buf)
        write_binary_uint64(CLICKHOUSE_DRIVER_VARIANT_BASIC_MODE, buf)

    def _write_data(self, items: Any, buf: Any) -> None:
        buf.write(
            bytes(
                CLICKHOUSE_VARIANT_NULL_DISCRIMINATOR if item is None else CLICKHOUSE_DYNAMIC_SHARED_DISCRIMINATOR
                for item in items
            )
        )
        for item in items:
            if item is not None:
                blob = ClickhouseSharedVariantValues.get_blob(item.value, item.column_type)
                write_varint(len(blob), buf)
                buf.write(blob)
