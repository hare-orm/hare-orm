from __future__ import annotations

import datetime
import time
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from clickhouse_driver import Client
from clickhouse_driver.result import QueryInfo

from hare.dialects.clickhouse.constants import CLICKHOUSE_TYPE_WRAPPERS
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_library_connection import (
    ClickhouseDriverLibraryConnection,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import (
    CLICKHOUSE_DRIVER_ARRAY_TYPE,
    CLICKHOUSE_DRIVER_MAP_TYPE,
    CLICKHOUSE_DRIVER_MICROSECOND_DIGITS,
    CLICKHOUSE_DRIVER_MICROSECONDS_PER_DAY,
    CLICKHOUSE_DRIVER_MICROSECONDS_PER_SECOND,
    CLICKHOUSE_DRIVER_MOMENT_TYPE_NAME,
    CLICKHOUSE_DRIVER_PING_AFTER_IDLE_SECONDS,
    CLICKHOUSE_DRIVER_SECONDS_MOMENT_TYPE,
    CLICKHOUSE_DRIVER_SECONDS_MOMENT_TYPE_PREFIX,
    CLICKHOUSE_DRIVER_TICKS_MOMENT_TYPE_PREFIX,
    CLICKHOUSE_DRIVER_TUPLE_TYPE,
)
from hare.dialects.clickhouse.drivers.constants import CLICKHOUSE_MOMENT_EPOCH
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.native.native_modules import NativeModules


class ClickhouseDriverLibraryClient(Client):  # type: ignore[misc]  # the library ships no types
    """clickhouse-driver's client as hare runs it:

    - the library pings the server before every statement; a connection that ran a statement within
      ``CLICKHOUSE_DRIVER_PING_AFTER_IDLE_SECONDS`` starts the next one without that round trip;
    - the moments of a binary insert are written as the whole ticks of the server's column, at any
      depth of an array, a map or a tuple - the library converts each through a time zone object and
      a float, and writes a moment before 1970 with a fraction of a second one second late;
    - the ProfileEvents block the server sends after each statement, which the library reads whole
      and drops, is passed over unread (``ClickhouseDriverLibraryConnection``).
    """

    #: When the connection started its last statement, on the monotonic clock.
    last_statement_time = 0.0
    #: ``rust.native.rows`` - converts a column's moments to ticks; None where it isn't built.
    native_rows: ClassVar[Any] = NativeModules.rows

    def get_connection(self) -> Any:
        connection = super().get_connection()
        # The library makes its connections itself - each one it hands out is made hare's.
        connection.__class__ = ClickhouseDriverLibraryConnection
        return connection

    def establish_connection(self, settings: dict[str, Any] | None) -> None:
        now = time.monotonic()
        connection = getattr(self, "connection", None)
        if (
            connection is None
            or not connection.connected
            or now - self.last_statement_time > CLICKHOUSE_DRIVER_PING_AFTER_IDLE_SECONDS
        ):
            super().establish_connection(settings)
        else:
            # What the library does after its ping answered.
            self.make_query_settings(settings)
            connection.check_query_execution()
            self.last_query = QueryInfo()
        self.last_statement_time = now

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
        moment_scale = cls._get_moment_scale(column_type)
        if moment_scale is not None:
            return cls._get_moment_ticks(values, moment_scale)
        if CLICKHOUSE_DRIVER_MOMENT_TYPE_NAME not in column_type:
            return values
        write_value = cls._get_value_writer(column_type)
        return values if write_value is None else [write_value(value) for value in values]

    @classmethod
    def _get_value_writer(cls, column_type: str) -> Callable[[Any], Any] | None:
        """How a value of a type holding moments is written - each moment in it as its ticks.

        Args:
            column_type: The server's type.

        Returns:
            The conversion of a value; None for a type holding no moment.
        """
        moment_scale = cls._get_moment_scale(column_type)
        if moment_scale is not None:
            return lambda value: None if value is None else cls._get_moment_ticks([value], moment_scale)[0]
        while column_type.startswith(CLICKHOUSE_TYPE_WRAPPERS):
            column_type = column_type[column_type.index("(") + 1 : -1]
        if CLICKHOUSE_DRIVER_MOMENT_TYPE_NAME not in column_type:
            return None
        type_name, arguments = ClickhouseTypeNames.get_type_parts(column_type)
        if type_name == CLICKHOUSE_DRIVER_ARRAY_TYPE:
            write_element = cls._get_value_writer(arguments[0])
            if write_element is None:
                return None
            return lambda value: None if value is None else [write_element(element) for element in value]
        if type_name == CLICKHOUSE_DRIVER_MAP_TYPE:
            write_key = cls._get_value_writer(arguments[0]) or cls._keep_value
            write_item = cls._get_value_writer(arguments[1]) or cls._keep_value
            return lambda value: (
                None if value is None else {write_key(key): write_item(item) for key, item in value.items()}
            )
        if type_name == CLICKHOUSE_DRIVER_TUPLE_TYPE:
            element_writers = [cls._get_value_writer(cls._get_element_type(argument)) for argument in arguments]
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
    def _get_element_type(element: str) -> str:
        """The type of an element of a tuple's type - its name before it left out.

        Args:
            element: The element - ``name Type`` or ``Type``.

        Returns:
            The type.
        """
        head, _separator, rest = element.strip().partition(" ")
        return rest.strip() if rest and "(" not in head else element.strip()

    @staticmethod
    def _get_moment_scale(column_type: str) -> int | None:
        """The fractional digits of a second a column of moments keeps.

        Args:
            column_type: The server's type of the column.

        Returns:
            The digits - 0 for a column of whole seconds - None for a column of anything else.
        """
        while column_type.startswith(CLICKHOUSE_TYPE_WRAPPERS):
            column_type = column_type[column_type.index("(") + 1 : -1]
        if column_type.startswith(CLICKHOUSE_DRIVER_TICKS_MOMENT_TYPE_PREFIX):
            parameters = column_type[len(CLICKHOUSE_DRIVER_TICKS_MOMENT_TYPE_PREFIX) : -1]
            return int(parameters.partition(",")[0])
        if column_type == CLICKHOUSE_DRIVER_SECONDS_MOMENT_TYPE or column_type.startswith(
            CLICKHOUSE_DRIVER_SECONDS_MOMENT_TYPE_PREFIX
        ):
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
        epoch = CLICKHOUSE_MOMENT_EPOCH
        if cls.native_rows is not None:
            ticks: list[Any] = cls.native_rows.get_moment_ticks(values, epoch, moment_scale)
            return ticks
        moment_class = datetime.datetime
        per_day = CLICKHOUSE_DRIVER_MICROSECONDS_PER_DAY
        per_second = CLICKHOUSE_DRIVER_MICROSECONDS_PER_SECOND
        microseconds = [
            (since_epoch := value - epoch).days * per_day + since_epoch.seconds * per_second + since_epoch.microseconds
            if isinstance(value, moment_class)
            else None
            for value in values
        ]
        if moment_scale > CLICKHOUSE_DRIVER_MICROSECOND_DIGITS:
            multiplier = 10 ** (moment_scale - CLICKHOUSE_DRIVER_MICROSECOND_DIGITS)
            return [
                value if moment is None else moment * multiplier
                for value, moment in zip(values, microseconds, strict=True)
            ]
        divisor = 10 ** (CLICKHOUSE_DRIVER_MICROSECOND_DIGITS - moment_scale)
        if divisor == 1 and None not in microseconds:
            return microseconds
        return [
            value if moment is None else moment // divisor for value, moment in zip(values, microseconds, strict=True)
        ]
