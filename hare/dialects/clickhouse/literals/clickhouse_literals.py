from __future__ import annotations

import datetime
import ipaddress
import json
import math
import uuid
from collections.abc import Callable, Sequence
from decimal import Decimal
from enum import Enum
from typing import Any

from hare.dialects.base.literals.constants import SQL_NULL_BYTE, SQL_NULL_BYTE_MESSAGE
from hare.dialects.base.literals.sql_literals import SqlLiterals
from hare.dialects.clickhouse.client.declarations import ClickhouseValueSet
from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_DATETIME_PRECISION,
    CLICKHOUSE_DECIMAL128_MAX_DIGITS,
    CLICKHOUSE_FIRST_YEAR,
    CLICKHOUSE_INTEGER_LITERAL_RANGE,
    CLICKHOUSE_LAST_YEAR,
    CLICKHOUSE_STRING_ESCAPES,
    CLICKHOUSE_TEMPORAL_RANGE_MESSAGE,
    CLICKHOUSE_WIDE_INTEGER_TYPES,
)
from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
from hare.exceptions import ValidationError
from hare.fields.data.containers.declarations import MapValue, TupleValue


class ClickhouseLiterals(SqlLiterals):
    """ClickHouse's literals - a backslash escapes inside a string, a moment is written in UTC with
    its microseconds, and a decimal, a date or a UUID keeps its type through a conversion function.
    The client binds every parameter of a statement through them."""

    def get_string_literal_sql(self, text: str) -> str:
        if SQL_NULL_BYTE in text:
            raise ValidationError(SQL_NULL_BYTE_MESSAGE.format(text=text))
        return "'" + text.translate(CLICKHOUSE_STRING_ESCAPES) + "'"

    def get_bytes_literal_sql(self, value: bytes) -> str:
        return f"unhex('{value.hex()}')"

    def get_array_literal_sql(self, element_sqls: Sequence[str]) -> str:
        return f"[{','.join(element_sqls)}]"

    def get_boolean_literal_sql(self, value: bool) -> str:
        return "true" if value else "false"

    def get_literal_writers(self) -> dict[type, Callable[[Any], str]]:
        return {
            **super().get_literal_writers(),
            bool: self.get_boolean_literal_sql,
            int: self.get_integer_literal_sql,
            Decimal: self.get_decimal_literal_sql,
            datetime.datetime: self.get_datetime_literal_sql,
            uuid.UUID: self.get_uuid_literal_sql,
            ipaddress.IPv4Address: self.get_ipv4_literal_sql,
            ipaddress.IPv6Address: self.get_ipv6_literal_sql,
            **dict.fromkeys((bytes, bytearray, memoryview), self.get_binary_literal_sql),
            ClickhouseTypedValue: self.get_typed_literal_sql,
            ClickhouseValueSet: self.get_value_set_literal_sql,
            TupleValue: self.get_tuple_literal_sql,
            MapValue: self.get_map_literal_sql,
            **dict.fromkeys((list, tuple), self.get_array_value_literal_sql),
            dict: self.get_json_literal_sql,
        }

    def find_literal_writer(self, value_class: type) -> Callable[[Any], str]:
        if issubclass(value_class, Enum):
            # An enum's member is written as its value - whatever else its class inherits.
            self.literal_writers_by_value_class[value_class] = self.get_enum_literal_sql
            return self.get_enum_literal_sql
        return super().find_literal_writer(value_class)

    def get_unknown_literal_sql(self, value: Any) -> str:
        return self.get_string_literal_sql(str(value))

    def get_enum_literal_sql(self, value: Enum) -> str:
        """An enum's member as its value.

        Args:
            value: The member.

        Returns:
            The literal.
        """
        return self.get_literal_sql(value.value)

    def get_integer_literal_sql(self, value: int) -> str:
        """An integer - one beyond 64 bits through ``get_wide_integer_literal_sql()``.

        Args:
            value: The integer.

        Returns:
            The literal.
        """
        if CLICKHOUSE_INTEGER_LITERAL_RANGE[0] <= value <= CLICKHOUSE_INTEGER_LITERAL_RANGE[1]:
            return str(value)
        return self.get_wide_integer_literal_sql(value)

    def get_float_literal_sql(self, value: float) -> str:
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)

    def get_date_literal_sql(self, value: datetime.date) -> str:
        if not CLICKHOUSE_FIRST_YEAR <= value.year <= CLICKHOUSE_LAST_YEAR:
            self.check_date_range(value)
        # A year from 1900 to 2299 has four digits - isoformat() writes the literal's text.
        return f"toDate32('{value.isoformat()}')"

    @staticmethod
    def get_uuid_literal_sql(value: uuid.UUID) -> str:
        """A UUID read from its text.

        Args:
            value: The UUID.

        Returns:
            The literal.
        """
        return f"toUUID('{value}')"

    @staticmethod
    def get_ipv4_literal_sql(value: ipaddress.IPv4Address) -> str:
        """An IPv4 address read from its text.

        Args:
            value: The address.

        Returns:
            The literal.
        """
        return f"toIPv4('{value}')"

    @staticmethod
    def get_ipv6_literal_sql(value: ipaddress.IPv6Address) -> str:
        """An IPv6 address read from its text.

        Args:
            value: The address.

        Returns:
            The literal.
        """
        return f"toIPv6('{value}')"

    def get_binary_literal_sql(self, value: bytes | bytearray | memoryview) -> str:
        """Bytes, whatever holds them.

        Args:
            value: The bytes.

        Returns:
            The literal.
        """
        return self.get_bytes_literal_sql(bytes(value))

    def get_value_set_literal_sql(self, value: ClickhouseValueSet) -> str:
        """A long ``IN`` list outside a read - the list it stands for, of values or of rows of them.

        Args:
            value: The set.

        Returns:
            The literal.
        """
        if len(value.column_types) == 1:
            return f"({','.join(self.get_literal_sql(item) for item in value.rows)})"
        rows_sql = ",".join(f"({','.join(self.get_literal_sql(item) for item in row)})" for row in value.rows)
        return f"({rows_sql})"

    def get_tuple_literal_sql(self, value: TupleValue) -> str:
        """A tuple's value.

        Args:
            value: The elements.

        Returns:
            The literal.
        """
        return f"tuple({','.join(self.get_literal_sql(element) for element in value)})"

    def get_map_literal_sql(self, value: MapValue) -> str:
        """A map's value.

        Args:
            value: The items.

        Returns:
            The literal.
        """
        pairs_sql = ",".join(
            f"{self.get_literal_sql(key)},{self.get_literal_sql(item)}" for key, item in value.items()
        )
        return f"map({pairs_sql})"

    def get_array_value_literal_sql(self, value: Sequence[Any]) -> str:
        """A list or a tuple as an array.

        Args:
            value: The elements.

        Returns:
            The literal.
        """
        return self.get_array_literal_sql([self.get_literal_sql(element) for element in value])

    def get_json_literal_sql(self, value: dict[Any, Any]) -> str:
        """A dict as the text of its JSON.

        Args:
            value: The dict.

        Returns:
            The literal.
        """
        return self.get_string_literal_sql(json.dumps(value, separators=(",", ":")))

    @staticmethod
    def get_wide_integer_literal_sql(value: int) -> str:
        """An integer beyond 64 bits, read from its text as the first wide integer type holding it - a
        number literal that wide is a ``Float64``.

        Args:
            value: The integer.

        Returns:
            The literal.

        Raises:
            ValidationError: No ClickHouse integer type holds it.
        """
        for type_name, low, high in CLICKHOUSE_WIDE_INTEGER_TYPES:
            if low <= value <= high:
                return f"to{type_name}('{value}')"
        raise ValidationError(f"{value} is outside every ClickHouse integer type")

    def get_typed_literal_sql(self, value: ClickhouseTypedValue) -> str:
        """A value of its own type - cast to it, an empty container as the type's default (ClickHouse
        casts no empty array to an array of ``Dynamic``), then cast to the ``Dynamic`` or ``Variant``
        type holding it.

        Args:
            value: The typed value.

        Returns:
            The literal.
        """
        column_type_sql = self.get_string_literal_sql(value.column_type)
        if value.value is None:
            value_sql = "NULL"
        elif isinstance(value.value, list | dict) and not value.value:
            value_sql = f"defaultValueOfTypeName({column_type_sql})"
        else:
            value_sql = f"CAST({self.get_literal_sql(value.value)}, {column_type_sql})"
        if value.held_type is None:
            return value_sql
        return f"CAST({value_sql}, {self.get_string_literal_sql(value.held_type)})"

    def get_decimal_literal_sql(self, value: Decimal) -> str:
        """A decimal as a ``Decimal128`` of its own scale (a ``Decimal256`` past its 38 digits) - a plain
        number literal would be a float.

        Args:
            value: The decimal.

        Returns:
            The literal.
        """
        if not value.is_finite():
            return self.get_literal_sql(float(value))
        _, digits, exponent = value.as_tuple()
        scale = -exponent if isinstance(exponent, int) and exponent < 0 else 0
        if len(digits) > CLICKHOUSE_DECIMAL128_MAX_DIGITS:
            return f"toDecimal256('{value:f}', {scale})"
        return f"toDecimal128('{value:f}', {scale})"

    @staticmethod
    def check_date_range(value: datetime.date) -> None:
        """Refuses a date ClickHouse doesn't store - its server would write the nearest one.

        Args:
            value: The date.

        Raises:
            ValidationError: The date is before 1900-01-01 or after 2299-12-31.
        """
        if not CLICKHOUSE_FIRST_YEAR <= value.year <= CLICKHOUSE_LAST_YEAR:
            raise ValidationError(CLICKHOUSE_TEMPORAL_RANGE_MESSAGE.format(value=value))

    @staticmethod
    def check_moment_range(value: datetime.datetime) -> None:
        """Refuses a moment ClickHouse doesn't store - its server would write the nearest one.

        Args:
            value: The moment, aware - a naive one is its UTC wall clock.

        Raises:
            ValidationError: The moment is before 1900-01-01 or after 2299-12-31 in UTC.
        """
        if value.tzinfo is not None:
            value = value.astimezone(datetime.UTC)
        if not CLICKHOUSE_FIRST_YEAR <= value.year <= CLICKHOUSE_LAST_YEAR:
            raise ValidationError(CLICKHOUSE_TEMPORAL_RANGE_MESSAGE.format(value=value))

    @staticmethod
    def get_datetime_literal_sql(value: datetime.datetime) -> str:
        """A moment as a ``DateTime64`` in UTC with its microseconds - a naive one is its UTC wall
        clock.

        Args:
            value: The moment.

        Returns:
            The literal.
        """
        if value.tzinfo is not None:
            value = value.astimezone(datetime.UTC).replace(tzinfo=None)
        # check_moment_range() of the moment in UTC, written out - one literal per moment written.
        if not CLICKHOUSE_FIRST_YEAR <= value.year <= CLICKHOUSE_LAST_YEAR:
            ClickhouseLiterals.check_moment_range(value)
        # A year from 1900 to 2299 has four digits - isoformat() writes the literal's text.
        return f"toDateTime64('{value.isoformat(' ', 'microseconds')}', {CLICKHOUSE_DATETIME_PRECISION}, 'UTC')"
