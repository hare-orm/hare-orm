from __future__ import annotations

import re
from typing import Any, ClassVar

from hare.dialects.base.schema.columns.column_conversion_check import ColumnConversionCheck
from hare.dialects.sqlite.enums import SqliteConvertedValueType
from hare.dialects.sqlite.schema.constants import (
    SQLITE_BOOLEAN_TEXT_SPELLINGS,
    SQLITE_INTEGER_TEXT_PATTERN,
    SQLITE_INVALID_INPUT_MESSAGE,
    SQLITE_NUMBER_TEXT_PATTERN,
    SQLITE_TEMPORAL_READER_FUNCTIONS,
)
from hare.exceptions import OperationalError
from hare.fields.data.boolean_field import BooleanField
from hare.fields.data.numeric.big_int_field import BigIntField
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.numeric.small_int_field import SmallIntField
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.data.text.char_field import CharField
from hare.fields.data.text.text_field import TextField
from hare.fields.field import Field


class SqliteColumnConversionCheck(ColumnConversionCheck):
    """The check of the values a column's type change converts - SQLite copies any value into the
    rebuilt table, and one the new field can't read would stay there unreadable: such a value raises
    ``OperationalError`` before the table changes."""

    __slots__ = ()

    #: The types whose values convert into each other - numbers and booleans.
    NUMBER_TYPES: ClassVar[frozenset[SqliteConvertedValueType]] = frozenset(
        {
            SqliteConvertedValueType.INTEGER,
            SqliteConvertedValueType.BIGINT,
            SqliteConvertedValueType.SMALLINT,
            SqliteConvertedValueType.DOUBLE_PRECISION,
            SqliteConvertedValueType.NUMERIC,
            SqliteConvertedValueType.BOOLEAN,
        }
    )

    async def check_alter_field_conversion(
        self, qualified_table: str, old_field: Field[Any], new_field: Field[Any], quoted_column: str
    ) -> None:
        """Refuses a column change when a stored value isn't one of the new field's type.

        Args:
            qualified_table: The already schema-qualified, quoted table name.
            old_field: The field's previous definition.
            new_field: Its new definition.
            quoted_column: The already-quoted column of the table being replaced.

        Raises:
            OperationalError: A stored value is no value of the new type.
        """
        old_type = self.get_value_type(old_field)
        new_type = self.get_value_type(new_field)
        if new_type is None and type(old_field) is not type(new_field):
            # A field of another type (a UUID, a JSON document, a duration) - each value read as it reads.
            unread_value = await self.get_unread_value(qualified_table, quoted_column, new_field)
            if unread_value is not None:
                raise OperationalError(
                    SQLITE_INVALID_INPUT_MESSAGE.format(new_type=type(new_field).__name__, value=unread_value)
                )
            return
        if new_type is None or old_type == new_type or new_type == SqliteConvertedValueType.TEXT:
            return
        if old_type in self.NUMBER_TYPES and new_type in self.NUMBER_TYPES:
            # A number or a boolean converts into each of them.
            return
        integer_range = new_field.COLUMN_TYPE_RANGE if isinstance(new_field, IntField) else None
        # A value of a type no conversion reads (a UUID, a JSON document) is its text - as a text's.
        unreadable_value = await self.get_unreadable_text(qualified_table, quoted_column, new_type, integer_range)
        if unreadable_value is not None:
            raise OperationalError(SQLITE_INVALID_INPUT_MESSAGE.format(new_type=new_type, value=unreadable_value))
        if new_type not in self.NUMBER_TYPES and (
            self.editor.table_rebuild.get_remake_value_conversion_sql(old_field, new_field, quoted_column)
            == quoted_column
        ):
            # Copied into the rebuilt table as it is stored - a date of a boolean, a time of a moment's text: each
            # value read as the new field reads it.
            unread_value = await self.get_unread_value(qualified_table, quoted_column, new_field)
            if unread_value is not None:
                raise OperationalError(
                    SQLITE_INVALID_INPUT_MESSAGE.format(new_type=type(new_field).__name__, value=unread_value)
                )

    @staticmethod
    def get_value_type(field: Field[Any]) -> SqliteConvertedValueType | None:
        """The type of a field's values a type change converts.

        Args:
            field: The field.

        Returns:
            The type, None for a field of another one - its change is left as it is.
        """
        if isinstance(field, BooleanField):
            return SqliteConvertedValueType.BOOLEAN
        if isinstance(field, BigIntField):
            return SqliteConvertedValueType.BIGINT
        if isinstance(field, SmallIntField):
            return SqliteConvertedValueType.SMALLINT
        if isinstance(field, IntField):
            return SqliteConvertedValueType.INTEGER
        if isinstance(field, FloatField):
            return SqliteConvertedValueType.DOUBLE_PRECISION
        if isinstance(field, DecimalField):
            return SqliteConvertedValueType.NUMERIC
        if isinstance(field, (CharField, TextField)):
            return SqliteConvertedValueType.TEXT
        if isinstance(field, DatetimeField):
            return SqliteConvertedValueType.TIMESTAMP
        if isinstance(field, DateField):
            return SqliteConvertedValueType.DATE
        if isinstance(field, TimeField):
            return SqliteConvertedValueType.TIME
        return None

    async def get_unreadable_text(
        self,
        qualified_table: str,
        quoted_column: str,
        new_type: SqliteConvertedValueType,
        integer_range: tuple[int, int] | None = None,
    ) -> str | None:
        """A stored value the new field doesn't read as a value of its type - by its text.

        Args:
            qualified_table: The already schema-qualified, quoted table name.
            quoted_column: The already-quoted column.
            new_type: The type the text is read as.
            integer_range: The lowest and the highest value of an integer column the text becomes - SQLite
                would keep a number beyond it, no read or write of the field taking it after.

        Returns:
            One such text, None when every one is read.
        """
        reader_function = SQLITE_TEMPORAL_READER_FUNCTIONS.get(new_type)
        client = self.editor.client
        if reader_function is not None:
            _, rows = await client.execute(
                f"SELECT {quoted_column} AS value FROM {qualified_table} "  # nosec B608
                f"WHERE {quoted_column} IS NOT NULL AND {reader_function}({quoted_column}) IS NULL LIMIT 1"
            )
            return str(dict(rows[0])["value"]) if rows else None
        _, rows = await client.execute(
            f"SELECT DISTINCT {quoted_column} AS value FROM {qualified_table} "  # nosec B608
            f"WHERE {quoted_column} IS NOT NULL"
        )
        for row in rows:
            value = str(dict(row)["value"])
            if not self.reads_text(value, new_type):
                return value
            if integer_range is not None and not integer_range[0] <= int(value) <= integer_range[1]:
                return value
        return None

    @staticmethod
    def reads_text(value: str, new_type: SqliteConvertedValueType) -> bool:
        """Whether a text is a number or a boolean.

        Args:
            value: The text.
            new_type: The type it is read as.

        Returns:
            Whether the text is a value of the type.
        """
        if new_type == SqliteConvertedValueType.BOOLEAN:
            return value.strip().lower() in SQLITE_BOOLEAN_TEXT_SPELLINGS
        if new_type in (SqliteConvertedValueType.DOUBLE_PRECISION, SqliteConvertedValueType.NUMERIC):
            return re.fullmatch(SQLITE_NUMBER_TEXT_PATTERN, value) is not None
        return re.fullmatch(SQLITE_INTEGER_TEXT_PATTERN, value) is not None
