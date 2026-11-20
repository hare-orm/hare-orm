from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

from clickhouse_connect.datatypes import dynamic
from clickhouse_connect.driver.common import write_leb128, write_uint64

from hare.dialects.clickhouse.drivers.clickhouse_shared_variant_values import ClickhouseSharedVariantValues
from hare.dialects.clickhouse.drivers.constants import (
    CLICKHOUSE_DYNAMIC_SHARED_DISCRIMINATOR,
    CLICKHOUSE_DYNAMIC_STRUCTURE_VERSION,
    CLICKHOUSE_DYNAMIC_WRITTEN_MAX_TYPES,
    CLICKHOUSE_VARIANT_NULL_DISCRIMINATOR,
)


class ClickhouseConnectDynamicType(dynamic.Dynamic, registered=False):
    """A client-local Dynamic codec preserving the types of shared-variant values."""

    def __init__(self, type_def: Any, types: Any) -> None:
        super().__init__(type_def)
        self.types = types

    @property
    def insert_name(self) -> str:
        return str(self.name)

    def read_column_prefix(self, source: Any, context: Any) -> Any:
        return self.types.read_dynamic_prefix(source, context)

    def write_column_prefix(self, dest: bytearray) -> None:
        """Writes the column's structure - no type of its own, discriminators one byte per row."""
        write_uint64(CLICKHOUSE_DYNAMIC_STRUCTURE_VERSION, dest)
        write_leb128(CLICKHOUSE_DYNAMIC_WRITTEN_MAX_TYPES, dest)
        write_leb128(0, dest)
        write_uint64(0, dest)

    def write_column_data(self, column: Sequence[Any], dest: bytearray, insert_context: Any) -> None:
        """Writes the values - each in the shared variant with its type, None as NULL."""
        dest += bytes(
            CLICKHOUSE_VARIANT_NULL_DISCRIMINATOR if value is None else CLICKHOUSE_DYNAMIC_SHARED_DISCRIMINATOR
            for value in column
        )
        for value in column:
            if value is not None:
                blob = ClickhouseSharedVariantValues.get_blob(value.value, value.type_name)
                write_leb128(len(blob), dest)
                dest += blob

    def _data_size(self, sample: Collection[Any]) -> int:
        """The bytes a value takes on average, as the library sizes its insert blocks - estimated."""
        return 32
