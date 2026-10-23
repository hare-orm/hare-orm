from __future__ import annotations

import re
from typing import Any

from hare.dialects.base.schema.columns.column_conversion_check import ColumnConversionCheck
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_APPLY_DELETED_MASK_TEMPLATE,
    CLICKHOUSE_COLUMN_TYPES_QUERY,
    CLICKHOUSE_CONVERTED_VALUE_TEMPLATE,
    CLICKHOUSE_DECIMAL_COLUMN_TYPE_PATTERN,
    CLICKHOUSE_DECIMAL_TEXT_TEMPLATE,
    CLICKHOUSE_ENUM_LABEL_PATTERN,
    CLICKHOUSE_NUMBER_COLUMN_TYPE_PATTERN,
    CLICKHOUSE_TEXT_COLUMN_TYPE_PATTERN,
    CLICKHOUSE_TYPE_NOT_CONVERTED_MESSAGE,
    CLICKHOUSE_TYPE_WRAPPER_PATTERN,
    CLICKHOUSE_UNCHECKED_CONVERSION_TYPE_PREFIXES,
    CLICKHOUSE_UNCONVERTED_DATE_TEMPLATE,
    CLICKHOUSE_UNCONVERTED_JSON_TEMPLATE,
    CLICKHOUSE_UNCONVERTED_MOMENT_TEMPLATE,
    CLICKHOUSE_UNCONVERTED_VALUE_QUERY,
    CLICKHOUSE_VALUE_NOT_CONVERTED_MESSAGE,
)
from hare.exceptions import OperationalError, UnSupportedError
from hare.fields.data.text.char_field import CharField
from hare.fields.data.text.text_field import TextField
from hare.fields.field import Field


class ClickhouseColumnConversionCheck(ColumnConversionCheck):
    """The conversion check of ClickHouse - a ``MODIFY COLUMN`` changes the column's type first and
    converts the stored values in a mutation after it: a value that doesn't convert fails the mutation
    and leaves the table unreadable, becomes NULL in a Nullable column, and an impossible date is read as
    another one. Such a value - a text that doesn't parse, a number of no date, a type the server
    converts to no value of the new one - raises ``OperationalError`` before the column changes."""

    __slots__ = ()

    async def check_alter_field_conversion(
        self, qualified_table: str, old_field: Field[Any], new_field: Field[Any], quoted_column: str
    ) -> None:
        client = self.editor.client
        # The checks below read no deleted row, which the MODIFY COLUMN would still convert.
        await self.editor.run_sql(CLICKHOUSE_APPLY_DELETED_MASK_TEMPLATE.format(table=qualified_table))
        types = await client.execute_dicts(
            CLICKHOUSE_COLUMN_TYPES_QUERY.format(
                column=quoted_column,
                table=qualified_table,
                new_type=client.dialect.literals.get_literal_sql(new_field.get_column_type(client.dialect)),
            )
        )
        if not types:
            return
        old_type, new_type = types[0]["old_type"], types[0]["new_type"]
        if (
            type(old_field) is not type(new_field)
            and not isinstance(new_field, (CharField, TextField))
            and re.fullmatch(CLICKHOUSE_TEXT_COLUMN_TYPE_PATTERN, new_type)
        ):
            # A field kept as text but read as another type (a time of day) - nothing converts its values,
            # each is read as the new field reads it.
            unread_value = await self.get_unread_value(qualified_table, quoted_column, new_field)
            if unread_value is not None:
                raise OperationalError(
                    CLICKHOUSE_VALUE_NOT_CONVERTED_MESSAGE.format(
                        column=quoted_column,
                        table=qualified_table,
                        value=unread_value,
                        new_type=type(new_field).__name__,
                    )
                )
            return
        if re.fullmatch(CLICKHOUSE_TEXT_COLUMN_TYPE_PATTERN, old_type) is not None:
            predicate_sql = self.get_unconverted_text_predicate_sql(quoted_column, new_type)
        else:
            predicate_sql = self.get_unconverted_value_predicate_sql(quoted_column, old_type, new_type)
        if predicate_sql is None:
            return
        try:
            rows = await client.execute_dicts(
                CLICKHOUSE_UNCONVERTED_VALUE_QUERY.format(
                    column=quoted_column, table=qualified_table, predicate=predicate_sql
                )
            )
        except (OperationalError, UnSupportedError) as error:
            # The server converts the column's type to no value of the new one.
            raise OperationalError(
                CLICKHOUSE_TYPE_NOT_CONVERTED_MESSAGE.format(
                    column=quoted_column, table=qualified_table, old_type=old_type, new_type=new_type, error=error
                )
            ) from error
        if rows:
            raise OperationalError(
                CLICKHOUSE_VALUE_NOT_CONVERTED_MESSAGE.format(
                    column=quoted_column, table=qualified_table, value=rows[0]["value"], new_type=new_type
                )
            )

    def get_unconverted_value_predicate_sql(self, quoted_column: str, old_type: str, new_type: str) -> str | None:
        """A WHERE predicate matching the rows whose value of a column of another type than text a
        column of the new type doesn't hold.

        Args:
            quoted_column: The quoted column.
            old_type: The column's type.
            new_type: The column's new type.

        Returns:
            The predicate, None for the same type of values, a type any value converts to, or two number
            types - the narrowing check checks those.
        """
        old_value_type, new_value_type = (self.get_value_type(type_name) for type_name in (old_type, new_type))
        if old_value_type == new_value_type or new_value_type.startswith(
            CLICKHOUSE_UNCHECKED_CONVERSION_TYPE_PREFIXES
        ):
            return None
        if (
            re.fullmatch(CLICKHOUSE_NUMBER_COLUMN_TYPE_PATTERN, old_value_type) is not None
            and re.fullmatch(CLICKHOUSE_NUMBER_COLUMN_TYPE_PATTERN, new_value_type) is not None
        ):
            return None
        value_sql = CLICKHOUSE_CONVERTED_VALUE_TEMPLATE.format(
            column=quoted_column, type=self.editor.client.dialect.literals.get_literal_sql(new_value_type)
        )
        return f"{value_sql} IS NULL"

    @staticmethod
    def get_value_type(type_name: str) -> str:
        """The type of a column's values, its ``Nullable``/``LowCardinality`` wrappers taken off.

        Args:
            type_name: The column's type.

        Returns:
            The type.
        """
        while (wrapper_match := re.fullmatch(CLICKHOUSE_TYPE_WRAPPER_PATTERN, type_name)) is not None:
            type_name = wrapper_match[1]
        return type_name

    def get_unconverted_text_predicate_sql(self, quoted_column: str, new_type: str) -> str | None:
        """A WHERE predicate matching the rows whose text a column of a type doesn't hold as written.

        Args:
            quoted_column: The quoted text column.
            new_type: The column's new type.

        Returns:
            The predicate, None for a type any text converts to or none is checked for.
        """
        if new_type.startswith(CLICKHOUSE_UNCHECKED_CONVERSION_TYPE_PREFIXES):
            return None
        if new_type == "JSON" or new_type.startswith("JSON("):
            return CLICKHOUSE_UNCONVERTED_JSON_TEMPLATE.format(column=quoted_column)
        if new_type.startswith(("Enum8(", "Enum16(")):
            labels_sql = ", ".join(re.findall(CLICKHOUSE_ENUM_LABEL_PATTERN, new_type))
            return f"{quoted_column} NOT IN ({labels_sql})"
        decimal_match = re.fullmatch(CLICKHOUSE_DECIMAL_COLUMN_TYPE_PATTERN, new_type)
        if decimal_match is not None:
            number_sql = CLICKHOUSE_DECIMAL_TEXT_TEMPLATE.format(column=quoted_column)
            overflow_sql = self.editor.column_narrowing_check.get_decimal_overflow_predicate_sql(
                number_sql, int(decimal_match[1]), int(decimal_match[2])
            )
            return f"{number_sql} IS NULL OR {overflow_sql}"
        value_sql = CLICKHOUSE_CONVERTED_VALUE_TEMPLATE.format(
            column=quoted_column, type=self.editor.client.dialect.literals.get_literal_sql(new_type)
        )
        if new_type in ("Date", "Date32"):
            return CLICKHOUSE_UNCONVERTED_DATE_TEMPLATE.format(value=value_sql, column=quoted_column)
        if new_type.startswith("DateTime"):
            return CLICKHOUSE_UNCONVERTED_MOMENT_TEMPLATE.format(value=value_sql, column=quoted_column)
        return f"{value_sql} IS NULL"
