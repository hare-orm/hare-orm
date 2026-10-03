from __future__ import annotations

import datetime
from typing import Any

from hare.dialects.base.types.type_mapping import TypeMapping
from hare.dialects.base.types.type_registry import TypeRegistry
from hare.fields.constants import NAIVE_INFINITY_DATETIMES
from hare.fields.data.binary import BinaryField
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.numeric.big_int_field import BigIntField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.numeric.small_int_field import SmallIntField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.data.uuids import UUIDField


class PostgresqlTypes:
    """How PostgreSQL stores hare's fields."""

    @staticmethod
    def build() -> TypeRegistry:
        """The PostgreSQL type registry.

        Returns:
            The registry.
        """
        types = TypeRegistry()
        types.register(IntField, TypeMapping(generated_sql="SERIAL NOT NULL PRIMARY KEY"))
        types.register(BigIntField, TypeMapping(generated_sql="BIGSERIAL NOT NULL PRIMARY KEY"))
        types.register(SmallIntField, TypeMapping(generated_sql="SMALLSERIAL NOT NULL PRIMARY KEY"))
        types.register(
            DatetimeField,
            TypeMapping(
                column_type="TIMESTAMPTZ",
                to_db=DatetimeField.to_db_instant_value,
                to_lookup=DatetimeField.to_db_instant_value,
                to_python=PostgresqlTypes.get_datetime_python_value,
                naive_datetime_is_utc=True,
            ),
        )
        types.register(TimeField, TypeMapping(column_type="TIMETZ"))
        types.register(JSONField, TypeMapping(column_type="JSONB"))
        types.register(UUIDField, TypeMapping(column_type="UUID"))
        types.register(BinaryField, TypeMapping(column_type="BYTEA"))
        return types

    @staticmethod
    def get_datetime_python_value(field: DatetimeField[Any], value: Any) -> Any:
        """Reads a datetime column; a naive value comes from a ``timestamp`` column, whose wall
        clock is UTC in hare's UTC sessions.

        Args:
            field: The field.
            value: The value the driver returned.

        Returns:
            The Python value.
        """
        if isinstance(value, datetime.datetime) and value.tzinfo is None and value not in NAIVE_INFINITY_DATETIMES:
            value = value.replace(tzinfo=datetime.UTC)
        return field.from_db_value(value)
