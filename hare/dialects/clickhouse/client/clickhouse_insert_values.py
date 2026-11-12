from __future__ import annotations

import datetime
import ipaddress
import json
import uuid
from collections.abc import Sequence
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.dialects.base.literals.constants import SQL_NULL_BYTE, SQL_NULL_BYTE_VALUE_MESSAGE
from hare.dialects.clickhouse.client.constants import (
    CLICKHOUSE_INSERTED_AS_IS_TYPES,
    CLICKHOUSE_INSERTED_MOMENT_COLUMN_TYPES,
    CLICKHOUSE_INSERTED_TEXT_COLUMN_TYPES,
    CLICKHOUSE_INSERTED_UNCHANGED_VALUE_TYPES,
)
from hare.dialects.clickhouse.constants import CLICKHOUSE_VALUE_OF_NO_TYPE_MESSAGE
from hare.dialects.clickhouse.literals.clickhouse_literals import ClickhouseLiterals
from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
from hare.exceptions import ValidationError
from hare.fields.data.containers.declarations import MapValue, TupleValue

if TYPE_CHECKING:
    from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient


class ClickhouseInsertValues:
    """Converts row values to the column sequences consumed by ClickHouse binary insert drivers."""

    @staticmethod
    def get_columns(client: type[ClickhouseClient], records: list[tuple[Any, ...]]) -> list[Sequence[Any]]:
        """The records turned into one sequence of values per column, each value as a driver's
        binary insert takes it.

        Args:
            records: One tuple of values per row.

        Returns:
            The columns, in the order of a record's values.

        Raises:
            ValidationError: A string holds a null byte, as where it is written into SQL text.
        """
        inserted_columns: list[Sequence[Any]] = []
        for column in zip(*records, strict=True):
            value_types = set(map(type, column))
            if value_types <= CLICKHOUSE_INSERTED_AS_IS_TYPES:
                if datetime.date in value_types:
                    ClickhouseInsertValues.check_date_column_range(column)
                inserted_columns.append(column)
            elif value_types <= CLICKHOUSE_INSERTED_TEXT_COLUMN_TYPES and SQL_NULL_BYTE not in "".join(
                filter(None, column)
            ):
                inserted_columns.append(column)
            elif value_types <= CLICKHOUSE_INSERTED_MOMENT_COLUMN_TYPES:
                # A naive moment is its UTC wall clock - a driver would read it as local time.
                moments = [
                    value if value is None or value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)
                    for value in column
                ]
                ClickhouseInsertValues.check_moment_column_range(moments)
                inserted_columns.append(moments)
            else:
                inserted_values = [ClickhouseInsertValues.get_value(client, value) for value in column]
                if bytes in value_types or bytearray in value_types or memoryview in value_types:
                    # A driver encodes a column of text or takes one of bytes, not a mix.
                    inserted_values = [
                        value.encode("utf-8") if isinstance(value, str) else value for value in inserted_values
                    ]
                inserted_columns.append(inserted_values)
        return inserted_columns

    @staticmethod
    def check_date_column_range(column: Sequence[Any]) -> None:
        """Refuses a column of dates holding one ClickHouse doesn't store - only its earliest
        and its latest date are checked.

        Args:
            column: Dates and None.

        Raises:
            ValidationError: A date is outside 1900-01-01 to 2299-12-31.
        """
        dates = [value for value in column if value is not None]
        if dates:
            ClickhouseLiterals.check_date_range(min(dates))
            ClickhouseLiterals.check_date_range(max(dates))

    @staticmethod
    def check_moment_column_range(column: Sequence[Any]) -> None:
        """Refuses a column of moments holding one ClickHouse doesn't store - only its earliest
        and its latest moment are checked.

        Args:
            column: Aware moments and None.

        Raises:
            ValidationError: A moment is outside 1900-01-01 to 2299-12-31 in UTC.
        """
        moments = [value for value in column if value is not None]
        if moments:
            ClickhouseLiterals.check_moment_range(min(moments))
            ClickhouseLiterals.check_moment_range(max(moments))

    @staticmethod
    def get_value(client: type[ClickhouseClient], value: Any) -> Any:
        """A value as a driver's binary insert takes it - the value its literal
        (``ClickhouseLiterals.get_literal_sql()``) writes into SQL text.

        Args:
            value: The value, ready for the database.

        Returns:
            The value to insert.

        Raises:
            ValidationError: A string holds a null byte, or a date or a moment is one ClickHouse
                doesn't store.
        """
        value_class = value.__class__
        if value_class in CLICKHOUSE_INSERTED_UNCHANGED_VALUE_TYPES:
            return value
        if value_class is ClickhouseTypedValue:
            return ClickhouseInsertValues.get_typed_value(client, value)
        while isinstance(value, Enum):
            value = value.value
        if value is None or isinstance(
            value, (bool, int, float, Decimal, uuid.UUID, ipaddress.IPv4Address, ipaddress.IPv6Address)
        ):
            return value
        if isinstance(value, str):
            if SQL_NULL_BYTE in value:
                raise ValidationError(SQL_NULL_BYTE_VALUE_MESSAGE)
            return value
        if isinstance(value, datetime.datetime):
            moment = value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)
            ClickhouseLiterals.check_moment_range(moment)
            return moment
        if isinstance(value, datetime.date):
            ClickhouseLiterals.check_date_range(value)
            return value
        if isinstance(value, datetime.time):
            return value.isoformat()
        if isinstance(value, datetime.timedelta):
            return client.dialect.literals.get_duration_text(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        return ClickhouseInsertValues.get_composite_value(client, value)

    @staticmethod
    def get_typed_value(client: type[ClickhouseClient], value: ClickhouseTypedValue) -> Any:
        """A value bound with its ClickHouse type as a driver's binary insert takes it.

        Args:
            value: The typed value.

        Returns:
            The value to insert - with its type when a ``Dynamic`` or ``Variant`` column holds it.
        """
        if value.value is None:
            return None
        inserted_value = ClickhouseInsertValues.get_value(client, value.value)
        if value.held_type is None:
            return inserted_value
        return client.get_inserted_typed_value(inserted_value, value.column_type)

    @staticmethod
    def get_composite_value(client: type[ClickhouseClient], value: Any) -> Any:
        """A value made of other values - or of a class no rule is known for - as a driver's binary
        insert takes it.

        Args:
            value: The value.

        Returns:
            The value to insert: each value inside as it is inserted, a dict as the text of its
            JSON.

        Raises:
            ValidationError: The value is of a class no ClickHouse type holds.
        """
        if isinstance(value, ClickhouseTypedValue):
            return ClickhouseInsertValues.get_typed_value(client, value)
        if isinstance(value, TupleValue):
            return tuple(ClickhouseInsertValues.get_value(client, element) for element in value)
        if isinstance(value, MapValue):
            return {
                ClickhouseInsertValues.get_value(client, key): ClickhouseInsertValues.get_value(client, item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [ClickhouseInsertValues.get_value(client, element) for element in value]
        if isinstance(value, dict):
            return json.dumps(value, separators=(",", ":"))
        raise ValidationError(
            CLICKHOUSE_VALUE_OF_NO_TYPE_MESSAGE.format(
                value=ValueDescriptions.get_shown_value(value), class_name=type(value).__name__
            )
        )
