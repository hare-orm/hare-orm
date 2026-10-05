from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.dialects.sqlite.enums import SqliteConvertedValueType
from hare.dialects.sqlite.schema.constants import (
    SQLITE_BOOLEAN_TEXT_SPELLINGS,
    SQLITE_INTEGER_TEXT_PATTERN,
    SQLITE_INVALID_INPUT_MESSAGE,
    SQLITE_NO_CAST_MESSAGE,
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


class SqliteColumnConversionCheck(SchemaEditorPart):
    """The check that a column's type change is one PostgreSQL makes - SQLite copies any value into
    the rebuilt table. A pair of types PostgreSQL has no cast between, and a text value the cast
    can't read, raise ``OperationalError`` in PostgreSQL's words before the table changes."""

    __slots__ = ()

    #: The number types - PostgreSQL casts each to each.
    NUMBER_TYPES: ClassVar[frozenset[SqliteConvertedValueType]] = frozenset(
        {
            SqliteConvertedValueType.INTEGER,
            SqliteConvertedValueType.BIGINT,
            SqliteConvertedValueType.SMALLINT,
            SqliteConvertedValueType.DOUBLE_PRECISION,
            SqliteConvertedValueType.NUMERIC,
        }
    )
    #: The casts between types other than numbers and text PostgreSQL makes.
    OTHER_CASTS: ClassVar[frozenset[tuple[SqliteConvertedValueType, SqliteConvertedValueType]]] = frozenset(
        {
            (SqliteConvertedValueType.INTEGER, SqliteConvertedValueType.BOOLEAN),
            (SqliteConvertedValueType.BOOLEAN, SqliteConvertedValueType.INTEGER),
            (SqliteConvertedValueType.DATE, SqliteConvertedValueType.TIMESTAMP),
            (SqliteConvertedValueType.TIMESTAMP, SqliteConvertedValueType.DATE),
            (SqliteConvertedValueType.TIMESTAMP, SqliteConvertedValueType.TIME),
        }
    )

    async def check_alter_field_conversion(
        self, qualified_table: str, old_field: Field[Any], new_field: Field[Any], quoted_column: str
    ) -> None:
        """Raise if the column's values can't be converted to the new field's type as PostgreSQL
        converts them.

        Args:
            qualified_table: The already schema-qualified, quoted table name.
            old_field: The field's previous definition.
            new_field: Its new definition.
            quoted_column: The already-quoted column of the table being replaced.

        Raises:
            OperationalError: PostgreSQL has no cast between the types, or a stored text is no
                value of the new type.
        """
        old_type = self.get_value_type(old_field)
        new_type = self.get_value_type(new_field)
        if old_type is None or new_type is None or old_type == new_type:
            return
        if not self.has_cast(old_type, new_type):
            raise OperationalError(SQLITE_NO_CAST_MESSAGE.format(old_type=old_type, new_type=new_type))
        if old_type == SqliteConvertedValueType.TEXT:
            unreadable_value = await self.get_unreadable_text(qualified_table, quoted_column, new_type)
            if unreadable_value is not None:
                raise OperationalError(SQLITE_INVALID_INPUT_MESSAGE.format(new_type=new_type, value=unreadable_value))

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

    def has_cast(self, old_type: SqliteConvertedValueType, new_type: SqliteConvertedValueType) -> bool:
        """Whether PostgreSQL casts values of one type to another.

        Args:
            old_type: The type converted from.
            new_type: The type converted to.

        Returns:
            True for numbers to numbers, anything to and from text, and the other casts it makes.
        """
        if SqliteConvertedValueType.TEXT in (old_type, new_type):
            return True
        if old_type in self.NUMBER_TYPES and new_type in self.NUMBER_TYPES:
            return True
        return (old_type, new_type) in self.OTHER_CASTS

    async def get_unreadable_text(
        self, qualified_table: str, quoted_column: str, new_type: SqliteConvertedValueType
    ) -> str | None:
        """A stored text PostgreSQL doesn't read as a value of a type.

        Args:
            qualified_table: The already schema-qualified, quoted table name.
            quoted_column: The already-quoted column.
            new_type: The type the text is read as.

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
        return None

    @staticmethod
    def reads_text(value: str, new_type: SqliteConvertedValueType) -> bool:
        """Whether PostgreSQL reads a text as a number or a boolean.

        Args:
            value: The text.
            new_type: The type it is read as.

        Returns:
            Whether the text is a value of the type.
        """
        if new_type == SqliteConvertedValueType.BOOLEAN:
            return value.strip().lower() in SQLITE_BOOLEAN_TEXT_SPELLINGS
        if new_type == SqliteConvertedValueType.DOUBLE_PRECISION:
            try:
                float(value)
            except ValueError:
                return False
            return True
        if new_type == SqliteConvertedValueType.NUMERIC:
            try:
                Decimal(value.strip())
            except InvalidOperation:
                return False
            return True
        return re.fullmatch(SQLITE_INTEGER_TEXT_PATTERN, value) is not None
