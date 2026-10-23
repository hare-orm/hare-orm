from __future__ import annotations

from collections.abc import Callable
from typing import Any

from clickhouse_driver.columns.dynamiccolumn import DynamicColumn
from clickhouse_driver.reader import read_binary_uint64
from clickhouse_driver.writer import write_binary_uint64

from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import CLICKHOUSE_DRIVER_VARIANT_BASIC_MODE
from hare.dialects.clickhouse.drivers.constants import CLICKHOUSE_VARIANT_NULL_DISCRIMINATOR


class ClickhouseDriverVariantColumn(DynamicColumn):  # type: ignore[misc]
    """clickhouse-driver's ``Variant(T, ...)`` column - read as the library reads a ``Dynamic`` column's
    values, a variant of the types it lists, with the types in the column's type instead; written from
    values bound with their type, each through the library's column of that type.

    Args:
        variant_types: The types of the variant, as the server sorts them.
        column_by_specification_getter: Builds the library's column of a type.
        **column_options: The library's column options.
    """

    prefix_needs_items = True

    def __init__(
        self, variant_types: list[str], column_by_specification_getter: Callable[[str], Any], **column_options: Any
    ) -> None:
        super().__init__(column_by_specification_getter, **column_options)
        self.variant_types = variant_types

    def read_state_prefix(self, buf: Any) -> None:
        self.variant_columns = [self.column_by_spec_getter(variant_type) for variant_type in self.variant_types]
        self._shared_variant_index = None
        self.discriminators_mode = read_binary_uint64(buf)
        if self.discriminators_mode != CLICKHOUSE_DRIVER_VARIANT_BASIC_MODE:
            raise NotImplementedError(
                f"A Variant column whose discriminators are sent in mode {self.discriminators_mode} isn't read"
            )
        for column in self.variant_columns:
            column.read_state_prefix(buf)

    def get_variant_items(self, items: Any) -> tuple[bytes, list[list[Any]]]:
        """Each row's discriminator, and the values of each type.

        Args:
            items: The values, each bound with its type, or None.

        Returns:
            The discriminators and the values per type.
        """
        discriminators = bytearray()
        variant_items: list[list[Any]] = [[] for _ in self.variant_types]
        for item in items:
            if item is None:
                discriminators.append(CLICKHOUSE_VARIANT_NULL_DISCRIMINATOR)
                continue
            index = self.variant_types.index(item.column_type)
            discriminators.append(index)
            variant_items[index].append(item.value)
        return bytes(discriminators), variant_items

    def write_state_prefix(self, buf: Any, items: Any = None) -> None:
        self.variant_columns = [self.column_by_spec_getter(variant_type) for variant_type in self.variant_types]
        write_binary_uint64(CLICKHOUSE_DRIVER_VARIANT_BASIC_MODE, buf)
        variant_items = self.get_variant_items(items or [])[1]
        for column, column_items in zip(self.variant_columns, variant_items, strict=True):
            column.write_state_prefix(buf, column_items)

    def _write_data(self, items: Any, buf: Any) -> None:
        discriminators, variant_items = self.get_variant_items(items)
        buf.write(discriminators)
        for column, column_items in zip(self.variant_columns, variant_items, strict=True):
            if column_items:
                column.write_data(column_items, buf)
