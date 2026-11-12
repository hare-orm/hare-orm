from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from clickhouse_driver import Client

from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_library_connection import (
    ClickhouseDriverLibraryConnection,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_query_result import (
    ClickhouseDriverQueryResult,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import CLICKHOUSE_DRIVER_MOMENT_TYPE_NAME
from hare.dialects.clickhouse.drivers.constants import CLICKHOUSE_MOMENT_EPOCH
from hare.dialects.clickhouse.enums import ClickhouseTypeName
from hare.dialects.clickhouse.types.clickhouse_physical_type import ClickhousePhysicalType
from hare.native.native_modules import NativeModules


class ClickhouseDriverLibraryClient(Client):  # type: ignore[misc]  # the library ships no types
    """Native-protocol client with local codecs and separate totals rows.

    Socket handling, pings, reconnection and service packets use the library lifecycle.
    Datetimes written in binary blocks are converted to exact whole ticks before encoding.
    """

    def __init__(self, **connection_settings: Any) -> None:
        # Client.__init__ creates an unopened ordinary connection and initializes its settings.
        # Replace that unused connection by construction, never by changing its Python class.
        super().__init__(**connection_settings)
        settings = {name: value for name, value in connection_settings.items() if name != "settings"}
        self.connection = ClickhouseDriverLibraryConnection(**settings)
        self.connection.context.settings = self.settings
        self.connection.context.client_settings = self.client_settings

    #: The totals row of the last statement read - None for a statement without one.
    last_totals: Any = None
    #: ``rust.native.rows`` - converts a column's moments to ticks.
    native_rows: ClassVar[Any] = NativeModules.rows

    def receive_result(self, with_column_types: bool = False, progress: bool = False, columnar: bool = False) -> Any:
        if progress:
            return super().receive_result(with_column_types=with_column_types, progress=progress, columnar=columnar)
        result = ClickhouseDriverQueryResult(
            self.packet_generator(), with_column_types=with_column_types, columnar=columnar
        )
        data = result.get_result()
        self.last_totals = result.totals_rows[0] if result.totals_rows else None
        return data

    def send_data(self, sample_block: Any, data: Any, types_check: bool = False, columnar: bool = False) -> int:
        if columnar:
            data = [
                self._get_written_values(values, column_type)
                for values, (_column_name, column_type) in zip(data, sample_block.columns_with_types, strict=True)
            ]
        return int(super().send_data(sample_block, data, types_check=types_check, columnar=columnar))

    @classmethod
    def _get_written_values(cls, values: Sequence[Any], column_type: str) -> Sequence[Any]:
        """A column's values as the library writes them exactly and without its slow conversion.

        Args:
            values: The column's values.
            column_type: The server's type of the column.

        Returns:
            The values to write - the same ones for a column of anything but moments.
        """
        if CLICKHOUSE_DRIVER_MOMENT_TYPE_NAME not in column_type:
            return values
        physical_type = ClickhousePhysicalType.from_sql(column_type)
        moment_scale = cls._get_moment_scale(physical_type)
        if moment_scale is not None:
            return cls._get_moment_ticks(values, moment_scale)
        write_value = cls._get_value_writer(physical_type)
        return values if write_value is None else [write_value(value) for value in values]

    @classmethod
    def _get_value_writer(cls, column_type: ClickhousePhysicalType) -> Callable[[Any], Any] | None:
        """How a value of a type holding moments is written - each moment in it as its ticks.

        Args:
            column_type: The server's type.

        Returns:
            The conversion of a value; None for a type holding no moment.
        """
        moment_scale = cls._get_moment_scale(column_type)
        if moment_scale is not None:
            return lambda value: None if value is None else cls._get_moment_ticks([value], moment_scale)[0]
        column_type = column_type.unwrapped
        if CLICKHOUSE_DRIVER_MOMENT_TYPE_NAME not in column_type.sql:
            return None
        if column_type.name == ClickhouseTypeName.ARRAY:
            write_element = cls._get_value_writer(column_type.elements[0])
            if write_element is None:
                return None
            return lambda value: None if value is None else [write_element(element) for element in value]
        if column_type.name == ClickhouseTypeName.MAP:
            write_key = cls._get_value_writer(column_type.elements[0]) or cls._keep_value
            write_item = cls._get_value_writer(column_type.elements[1]) or cls._keep_value
            return lambda value: (
                None if value is None else {write_key(key): write_item(item) for key, item in value.items()}
            )
        if column_type.name == ClickhouseTypeName.TUPLE:
            element_writers = [cls._get_value_writer(element) for element in column_type.elements]
            if all(write_element is None for write_element in element_writers):
                return None
            writers = [write_element or cls._keep_value for write_element in element_writers]
            return lambda value: (
                None
                if value is None
                else tuple(write_element(element) for write_element, element in zip(writers, value, strict=True))
            )
        return None

    @staticmethod
    def _keep_value(value: Any) -> Any:
        """A value written as it is.

        Args:
            value: The value.

        Returns:
            The value.
        """
        return value

    @staticmethod
    def _get_moment_scale(column_type: ClickhousePhysicalType) -> int | None:
        """The fractional digits of a second a column of moments keeps.

        Args:
            column_type: The server's type of the column.

        Returns:
            The digits - 0 for a column of whole seconds - None for a column of anything else.
        """
        column_type = column_type.unwrapped
        if column_type.name == ClickhouseTypeName.DATETIME64:
            return column_type.precision
        if column_type.name == CLICKHOUSE_DRIVER_MOMENT_TYPE_NAME:
            return 0
        return None

    @classmethod
    def _get_moment_ticks(cls, values: Sequence[Any], moment_scale: int) -> list[Any]:
        """The values of a column of moments, each aware moment as the ticks since 1970 the column
        stores - what is finer than a tick is dropped, as the server drops it from a literal.

        Args:
            values: The column's values - aware moments, None, or ticks already.
            moment_scale: The fractional digits of a second the column keeps.

        Returns:
            The values to write.
        """
        ticks: list[Any] = cls.native_rows.get_moment_ticks(values, CLICKHOUSE_MOMENT_EPOCH, moment_scale)
        return ticks
