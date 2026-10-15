from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_NOT_NULL_CHANGE_REFUSED_MESSAGE,
    CLICKHOUSE_WIDE_DECIMAL_BOUND_TEMPLATE,
)
from hare.exceptions import IntegrityError
from hare.fields.enums import HeldValueStep, NarrowedValueSource
from hare.fields.narrowing.enum_values_limit import EnumValuesLimit
from hare.fields.narrowing.integer_range_limit import IntegerRangeLimit
from hare.fields.narrowing.text_length_limit import TextLengthLimit

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.data.containers.container_field import HeldValuePath
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit


class ClickhouseColumnNarrowingCheck(ColumnNarrowingCheck):
    """The narrowing check of ClickHouse - a ``MODIFY COLUMN`` wraps an integer around, cuts a decimal's
    digits and keeps a too long text without an error. A text's length is counted in characters, a
    number compared as it is stored, and a value held in a container at any depth is reached through
    ``arrayExists()`` of its elements, keys or values, or ``tupleElement()``."""

    __slots__ = ()

    async def check_not_null_change(self, qualified_table: str, quoted_column: str) -> None:
        # The MODIFY COLUMN of a column holding NULL fails in its mutation, which stays and blocks
        # every later one - checked first, refused as PostgreSQL refuses it.
        rows = await self.editor.client.execute_dicts(
            f"SELECT count(*) AS null_count FROM {qualified_table} WHERE {quoted_column} IS NULL"  # nosec B608
        )
        if rows[0]["null_count"]:
            raise IntegrityError(
                CLICKHOUSE_NOT_NULL_CHANGE_REFUSED_MESSAGE.format(column=quoted_column, table=qualified_table)
            )

    def get_text_overflow_predicate_sql(self, limit: TextLengthLimit, quoted_column: str) -> str:
        if limit.source == NarrowedValueSource.TEXT:
            text_sql = quoted_column
        elif limit.source == NarrowedValueSource.BOOLEAN:
            text_sql = f"if({quoted_column}, 'true', 'false')"
        else:
            text_sql = f"toString({quoted_column})"
        return f"lengthUTF8({text_sql}) > {limit.max_length}"

    def get_integer_overflow_predicate_sql(self, limit: IntegerRangeLimit, quoted_column: str) -> str:
        if not limit.checks_fraction:
            return f"{quoted_column} < {limit.lowest} OR {quoted_column} > {limit.highest}"
        # A Decimal column can't be compared with an integer bound past its own digits.
        lowest_sql = CLICKHOUSE_WIDE_DECIMAL_BOUND_TEMPLATE.format(limit.lowest)
        highest_sql = CLICKHOUSE_WIDE_DECIMAL_BOUND_TEMPLATE.format(limit.highest)
        return (
            f"round({quoted_column}, 0) != {quoted_column} "
            f"OR {quoted_column} < {lowest_sql} OR {quoted_column} > {highest_sql}"
        )

    def get_enum_overflow_predicate_sql(self, limit: EnumValuesLimit, quoted_column: str) -> str:
        # An Enum column is compared by its labels - an IntEnumField's are its members' names.
        literals = self.editor.client.dialect.literals
        if all(isinstance(value, int) for value in limit.values):
            values_sql = ", ".join(str(value) for value in sorted(limit.values))
            return f"CAST({quoted_column} AS Int64) NOT IN ({values_sql})"
        values_sql = ", ".join(literals.get_literal_sql(value) for value in sorted(limit.values))
        return f"toString({quoted_column}) NOT IN ({values_sql})"

    def get_decimal_overflow_predicate_sql(self, quoted_column: str, max_digits: int, decimal_places: int) -> str:
        whole_digits_limit = "1" + "0" * (max_digits - decimal_places)
        return (
            f"round({quoted_column}, {decimal_places}) != {quoted_column} "
            f"OR abs({quoted_column}) >= {whole_digits_limit}"
        )

    def get_held_value_overflow_predicate_sql(
        self, held_path: HeldValuePath, limit: NarrowingLimit, quoted_column: str
    ) -> str:
        step, position = held_path[0]
        if step == HeldValueStep.TUPLE_ELEMENT:
            element_sql = f"tupleElement({quoted_column}, {(position or 0) + 1})"
            if len(held_path) == 1:
                return self.get_narrowing_overflow_predicate_sql(limit, element_sql)
            return self.get_held_value_overflow_predicate_sql(held_path[1:], limit, element_sql)
        values_sql = {
            HeldValueStep.ELEMENT: quoted_column,
            HeldValueStep.KEY: f"mapKeys({quoted_column})",
            HeldValueStep.VALUE: f"mapValues({quoted_column})",
        }[step]
        # A name per depth - a lambda inside a lambda reads the value of its own depth.
        value_sql = f"hare_held_value_{len(held_path)}"
        if len(held_path) == 1:
            predicate_sql = self.get_narrowing_overflow_predicate_sql(limit, value_sql)
        else:
            predicate_sql = self.get_held_value_overflow_predicate_sql(held_path[1:], limit, value_sql)
        return f"arrayExists({value_sql} -> {predicate_sql}, {values_sql})"
