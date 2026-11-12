from __future__ import annotations

from typing import Any

from hare.dialects.base.schema.columns.column_conversion_check import ColumnConversionCheck
from hare.dialects.clickhouse.enums import ClickhouseTypeName
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_APPLY_DELETED_MASK_TEMPLATE,
    CLICKHOUSE_COLUMN_TYPES_QUERY,
    CLICKHOUSE_CONVERTED_VALUE_TEMPLATE,
    CLICKHOUSE_DECIMAL_TEXT_TEMPLATE,
    CLICKHOUSE_NUMBER_COLUMN_TYPES,
    CLICKHOUSE_TEXT_COLUMN_TYPES,
    CLICKHOUSE_TYPE_NOT_CONVERTED_MESSAGE,
    CLICKHOUSE_UNCHECKED_CONVERSION_TYPES,
    CLICKHOUSE_UNCONVERTED_DATE_TEMPLATE,
    CLICKHOUSE_UNCONVERTED_JSON_TEMPLATE,
    CLICKHOUSE_UNCONVERTED_MOMENT_TEMPLATE,
    CLICKHOUSE_UNCONVERTED_VALUE_QUERY,
    CLICKHOUSE_VALUE_NOT_CONVERTED_MESSAGE,
)
from hare.dialects.clickhouse.types.clickhouse_physical_type import ClickhousePhysicalType
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
        old_physical_type, new_physical_type = (
            ClickhousePhysicalType.from_sql(type_name).unwrapped for type_name in (old_type, new_type)
        )
        if (
            type(old_field) is not type(new_field)
            and not isinstance(new_field, (CharField, TextField))
            and new_physical_type.name in CLICKHOUSE_TEXT_COLUMN_TYPES
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
        if old_physical_type.name in CLICKHOUSE_TEXT_COLUMN_TYPES:
            predicate_sql = self.get_unconverted_text_predicate_sql(quoted_column, new_physical_type)
        else:
            predicate_sql = self.get_unconverted_value_predicate_sql(
                quoted_column, old_physical_type, new_physical_type
            )
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

    def get_unconverted_value_predicate_sql(
        self, quoted_column: str, old_type: ClickhousePhysicalType, new_type: ClickhousePhysicalType
    ) -> str | None:
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
        old_value_type, new_value_type = old_type.unwrapped, new_type.unwrapped
        if old_value_type.sql == new_value_type.sql or new_value_type.name in CLICKHOUSE_UNCHECKED_CONVERSION_TYPES:
            return None
        if (
            old_value_type.name in CLICKHOUSE_NUMBER_COLUMN_TYPES
            and new_value_type.name in CLICKHOUSE_NUMBER_COLUMN_TYPES
        ):
            return None
        value_sql = CLICKHOUSE_CONVERTED_VALUE_TEMPLATE.format(
            column=quoted_column, type=self.editor.client.dialect.literals.get_literal_sql(new_value_type.sql)
        )
        return f"{value_sql} IS NULL"

    def get_unconverted_text_predicate_sql(self, quoted_column: str, new_type: ClickhousePhysicalType) -> str | None:
        """A WHERE predicate matching the rows whose text a column of a type doesn't hold as written.

        Args:
            quoted_column: The quoted text column.
            new_type: The column's new type.

        Returns:
            The predicate, None for a type any text converts to or none is checked for.
        """
        physical = new_type.unwrapped
        if physical.name in CLICKHOUSE_UNCHECKED_CONVERSION_TYPES:
            return None
        if physical.name == ClickhouseTypeName.JSON:
            return CLICKHOUSE_UNCONVERTED_JSON_TEMPLATE.format(column=quoted_column)
        if physical.name in {ClickhouseTypeName.ENUM8, ClickhouseTypeName.ENUM16}:
            literals = self.editor.client.dialect.literals
            labels_sql = ", ".join(
                literals.get_string_literal_sql(label) for label in physical.get_enum_members(physical.arguments)
            )
            return f"{quoted_column} NOT IN ({labels_sql})"
        if physical.precision is not None and physical.scale is not None:
            number_sql = CLICKHOUSE_DECIMAL_TEXT_TEMPLATE.format(column=quoted_column)
            overflow_sql = self.editor.column_narrowing_check.get_decimal_overflow_predicate_sql(
                number_sql, physical.precision, physical.scale
            )
            return f"{number_sql} IS NULL OR {overflow_sql}"
        value_sql = CLICKHOUSE_CONVERTED_VALUE_TEMPLATE.format(
            column=quoted_column, type=self.editor.client.dialect.literals.get_literal_sql(physical.sql)
        )
        if physical.name in {ClickhouseTypeName.DATE, ClickhouseTypeName.DATE32}:
            return CLICKHOUSE_UNCONVERTED_DATE_TEMPLATE.format(value=value_sql, column=quoted_column)
        if physical.name in {ClickhouseTypeName.DATETIME, ClickhouseTypeName.DATETIME64}:
            return CLICKHOUSE_UNCONVERTED_MOMENT_TEMPLATE.format(value=value_sql, column=quoted_column)
        return f"{value_sql} IS NULL"
