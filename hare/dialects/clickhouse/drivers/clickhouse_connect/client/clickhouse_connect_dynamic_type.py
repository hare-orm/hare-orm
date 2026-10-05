from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from clickhouse_connect.datatypes import dynamic
from clickhouse_connect.driver.common import write_leb128, write_uint64

from hare.dialects.clickhouse.drivers.clickhouse_shared_variant_values import ClickhouseSharedVariantValues
from hare.dialects.clickhouse.drivers.constants import (
    CLICKHOUSE_DYNAMIC_SHARED_DISCRIMINATOR,
    CLICKHOUSE_DYNAMIC_STRUCTURE_VERSION,
    CLICKHOUSE_DYNAMIC_WRITTEN_MAX_TYPES,
    CLICKHOUSE_VARIANT_NULL_DISCRIMINATOR,
)


class ClickhouseConnectDynamicType:
    """clickhouse-connect's ``Dynamic`` type, made to write and read values of their own types: the
    library inserts a ``Dynamic`` column as text (``5`` as the string ``'5'``, None as ``'NULL'``) and
    reads no decimal it finds in the shared variant. A value is written into the shared variant with its
    type, and a value of the shared variant read through ``ClickhouseSharedVariantValues``."""

    #: Whether the library's type was replaced.
    installed: ClassVar[list[bool]] = []

    @classmethod
    def install(cls) -> None:
        """Replaces the library's writing and reading - once."""
        if cls.installed:
            return
        cls.installed.append(True)
        # Typed Any: the library's own types are there only when it is installed with them.
        dynamic_type_class: Any = dynamic.Dynamic
        dynamic_type_class.insert_name = property(cls.get_insert_name)
        dynamic_type_class.write_column_prefix = cls.write_column_prefix
        dynamic_type_class.write_column_data = cls.write_column_data
        dynamic_type_class._data_size = cls.get_data_size
        dynamic_module: Any = dynamic
        dynamic_module.decode_shared_variant_value = cls.decode_shared_variant_value

    @staticmethod
    def get_insert_name(dynamic_type: Any) -> str:
        """The type a column is inserted as - its own."""
        return str(dynamic_type.name)

    @staticmethod
    def write_column_prefix(dynamic_type: Any, dest: bytearray) -> None:
        """Writes the column's structure - no type of its own, discriminators one byte per row."""
        write_uint64(CLICKHOUSE_DYNAMIC_STRUCTURE_VERSION, dest)
        write_leb128(CLICKHOUSE_DYNAMIC_WRITTEN_MAX_TYPES, dest)
        write_leb128(0, dest)
        write_uint64(0, dest)

    @staticmethod
    def write_column_data(dynamic_type: Any, column: Sequence[Any], dest: bytearray, insert_context: Any) -> None:
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

    @staticmethod
    def get_data_size(dynamic_type: Any, sample: Sequence[Any]) -> int:
        """The bytes a value takes on average, as the library sizes its insert blocks - estimated."""
        return 32

    @staticmethod
    def decode_shared_variant_value(binary_data: bytes | None, query_context: Any) -> Any:
        """The Python value of a value of the shared variant."""
        return None if binary_data is None else ClickhouseSharedVariantValues.decode(binary_data)
