from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck
from hare.dialects.clickhouse.fields.constants import UINT64_RANGE
from hare.dialects.clickhouse.fields.fixed_string_field import FixedStringField
from hare.dialects.clickhouse.fields.narrowing.byte_length_limit import ByteLengthLimit
from hare.dialects.clickhouse.fields.narrowing.moment_limit import MomentLimit
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_FIXED_STRING_TEXT_TEMPLATE,
    CLICKHOUSE_HELD_ELEMENT_NAME,
    CLICKHOUSE_NOT_NULL_CHANGE_REFUSED_MESSAGE,
    CLICKHOUSE_WIDE_DECIMAL_BOUND_TEMPLATE,
)
from hare.exceptions import IntegrityError
from hare.fields.data.constants import INT64_MIN
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.data.containers.tuple_field import TupleField
from hare.fields.data.text.char_field import CharField
from hare.fields.data.text.text_field import TextField
from hare.fields.enums import HeldValueStep, NarrowedValueSource
from hare.fields.narrowing.constants import FLOAT32_MANTISSA_BITS
from hare.fields.narrowing.enum_values_limit import EnumValuesLimit
from hare.fields.narrowing.float_precision_limit import FloatPrecisionLimit
from hare.fields.narrowing.integer_range_limit import IntegerRangeLimit
from hare.fields.narrowing.text_length_limit import TextLengthLimit
from hare.migrations.exceptions import FieldNarrowingDataLossError

if TYPE_CHECKING:
    from hare.dialects.clickhouse.literals.clickhouse_literals import ClickhouseLiterals
    from hare.fields.data.containers.container_field import HeldValuePath
    from hare.fields.field import Field
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit


class ClickhouseColumnNarrowingCheck(ColumnNarrowingCheck):
    """The narrowing check of ClickHouse - a ``MODIFY COLUMN`` wraps an integer around, cuts a decimal's
    digits and keeps a too long text without an error. A text's length is counted in characters, a
    number compared as it is stored, and a value held in a container at any depth is reached through
    ``arrayExists()`` of its elements, keys or values, or ``tupleElement()``."""

    __slots__ = ()

    async def check_alter_field_narrowing_data_loss(
        self, qualified_table: str, old_field: Field[Any], new_field: Field[Any], quoted_column: str
    ) -> None:
        # A text becoming a number or a date is checked by the conversion check, by the value of its text -
        # these predicates compare a stored number.
        if isinstance(old_field, (CharField, TextField)) and not isinstance(
            new_field, (CharField, TextField, FixedStringField)
        ):
            return
        # A FixedString holds its text padded to its width with zero bytes - its length is of the text.
        value_sql = (
            CLICKHOUSE_FIXED_STRING_TEXT_TEMPLATE.format(column=quoted_column)
            if isinstance(old_field, FixedStringField)
            else quoted_column
        )
        await super().check_alter_field_narrowing_data_loss(qualified_table, old_field, new_field, value_sql)
        null_value_predicate_sql = ClickhouseColumnNarrowingCheck.get_held_null_predicate_sql(
            old_field, new_field, quoted_column
        )
        if null_value_predicate_sql is not None:
            # The change's mutation fails on a NULL element after the column took its new type, which no
            # row can be read by then.
            rows = await self.editor.client.execute_dicts(
                f"SELECT count(*) AS null_count FROM {qualified_table} "  # nosec B608
                f"WHERE {null_value_predicate_sql}"
            )
            null_count = rows[0]["null_count"]
            if null_count:
                raise FieldNarrowingDataLossError(
                    f"Cannot narrow column {quoted_column} on {qualified_table} to the new definition of field "
                    f"{new_field.model_field_name!r} - {null_count} existing row(s) hold a NULL element its "
                    "elements take no more. Clean up the offending values by hand first, then retry the migration."
                )

    @staticmethod
    def get_held_null_predicate_sql(
        old_field: Field[Any], new_field: Field[Any], value_sql: str, depth: int = 0
    ) -> str | None:
        """A WHERE predicate matching the rows holding NULL where a change of a container field takes it no
        more - an element of an array, a member of a tuple (a row of a Nested field), at any depth.

        Args:
            old_field: The field before the change.
            new_field: The field after it.
            value_sql: The SQL of the value the fields hold.
            depth: How deep in the column the value is - the column itself at 0.

        Returns:
            The predicate, None when no value held takes NULL no more.
        """
        if isinstance(old_field, ArrayField) and isinstance(new_field, ArrayField):
            element_sql = f"{CLICKHOUSE_HELD_ELEMENT_NAME}_{depth}"
            element_predicate_sql = ClickhouseColumnNarrowingCheck.get_held_null_predicate_sql(
                old_field.base_field, new_field.base_field, element_sql, depth + 1
            )
            if element_predicate_sql is None:
                return None
            return f"arrayExists({element_sql} -> {element_predicate_sql}, {value_sql})"
        if isinstance(old_field, TupleField) and isinstance(new_field, TupleField):
            member_predicate_sqls = [
                member_predicate_sql
                for position, (old_member, new_member) in enumerate(
                    zip(old_field.fields_in_order, new_field.fields_in_order, strict=False), start=1
                )
                if (
                    member_predicate_sql := ClickhouseColumnNarrowingCheck.get_held_null_predicate_sql(
                        old_member, new_member, f"tupleElement({value_sql}, {position})", depth + 1
                    )
                )
                is not None
            ]
            return " OR ".join(f"({sql})" for sql in member_predicate_sqls) or None
        if depth and old_field.null and not new_field.null:
            return f"isNull({value_sql})"
        return None

    async def check_not_null_change(self, qualified_table: str, quoted_column: str) -> None:
        # The MODIFY COLUMN of a column holding NULL fails in its mutation, which stays and blocks
        # every later one - checked first and refused.
        rows = await self.editor.client.execute_dicts(
            f"SELECT count(*) AS null_count FROM {qualified_table} WHERE {quoted_column} IS NULL"  # nosec B608
        )
        if rows[0]["null_count"]:
            raise IntegrityError(
                CLICKHOUSE_NOT_NULL_CHANGE_REFUSED_MESSAGE.format(column=quoted_column, table=qualified_table)
            )

    def get_text_overflow_predicate_sql(self, limit: TextLengthLimit, quoted_column: str) -> str:
        # lengthUTF8() counts characters.
        return f"lengthUTF8({self.get_value_text_sql(limit.source, quoted_column)}) > {limit.max_length}"

    def get_byte_length_overflow_predicate_sql(self, limit: ByteLengthLimit, quoted_column: str) -> str:
        """A WHERE predicate matching the rows whose value's text takes more bytes than a limit.

        Args:
            limit: The limit.
            quoted_column: The quoted column.

        Returns:
            The predicate.
        """
        # length() counts bytes.
        return f"length({self.get_value_text_sql(limit.source, quoted_column)}) > {limit.max_length}"

    @staticmethod
    def get_value_text_sql(source: NarrowedValueSource, quoted_column: str) -> str:
        """A value of the column as the text a narrower text column would hold.

        Args:
            source: What the column holds.
            quoted_column: The quoted column.

        Returns:
            The SQL expression.
        """
        if source == NarrowedValueSource.TEXT:
            return quoted_column
        if source == NarrowedValueSource.BOOLEAN:
            return f"if({quoted_column}, 'true', 'false')"
        return f"toString({quoted_column})"

    def get_integer_overflow_predicate_sql(self, limit: IntegerRangeLimit, quoted_column: str) -> str:
        if not limit.checks_fraction:
            # A number literal past 64 bits is a Float64 - such a bound is read from its text.
            lowest_sql, highest_sql = (
                str(bound)
                if INT64_MIN <= bound <= UINT64_RANGE[1]
                else cast("ClickhouseLiterals", self.editor.client.dialect.literals).get_wide_integer_literal_sql(
                    bound
                )
                for bound in (limit.lowest, limit.highest)
            )
            return f"{quoted_column} < {lowest_sql} OR {quoted_column} > {highest_sql}"
        # A Decimal column can't be compared with an integer bound past its own digits.
        lowest_sql = CLICKHOUSE_WIDE_DECIMAL_BOUND_TEMPLATE.format(limit.lowest)
        highest_sql = CLICKHOUSE_WIDE_DECIMAL_BOUND_TEMPLATE.format(limit.highest)
        return (
            f"round({quoted_column}, 0) != {quoted_column} "
            f"OR {quoted_column} < {lowest_sql} OR {quoted_column} > {highest_sql}"
        )

    def get_float_overflow_predicate_sql(self, limit: FloatPrecisionLimit, quoted_column: str) -> str:
        float_function = "toFloat64" if limit.mantissa_bits > FLOAT32_MANTISSA_BITS else "toFloat32"
        float_sql = f"toFloat64({float_function}({quoted_column}))"
        if limit.decimal_places is None:
            # A float compares with an integer or a float exactly.
            return f"{float_sql} != {quoted_column}"
        # A Decimal compares with a float as a float - compared as the decimal of the float's text.
        return f"toDecimal256(toString({float_sql}), {limit.decimal_places}) != {quoted_column}"

    def get_time_of_day_predicate_sql(self, quoted_column: str) -> str:
        return f"toStartOfDay({quoted_column}) != {quoted_column}"

    def get_moment_overflow_predicate_sql(self, limit: MomentLimit, quoted_column: str) -> str:
        """A WHERE predicate matching the rows whose moment is outside the limit's or has more digits after the
        second.

        Args:
            limit: The limit.
            quoted_column: The quoted column.

        Returns:
            The predicate.
        """
        literals = self.editor.client.dialect.literals
        return (
            f"toDateTime64({quoted_column}, {limit.digits}) != {quoted_column} "
            f"OR {quoted_column} < {literals.get_literal_sql(limit.first_moment)} "
            f"OR {quoted_column} > {literals.get_literal_sql(limit.last_moment)}"
        )

    def get_enum_overflow_predicate_sql(self, limit: EnumValuesLimit, quoted_column: str) -> str:
        # An Enum column is compared by its labels - an IntEnumField's are its members' names.
        literals = self.editor.client.dialect.literals
        if all(isinstance(value, int) for value in limit.values):
            values_sql = ", ".join(str(value) for value in sorted(limit.values))
            # Nullable - a NULL row has no member to be out of, and converts to no plain Int64.
            return f"CAST({quoted_column} AS Nullable(Int64)) NOT IN ({values_sql})"
        values_sql = ", ".join(literals.get_literal_sql(value) for value in sorted(limit.values))
        return f"toString({quoted_column}) NOT IN ({values_sql})"

    def get_decimal_overflow_predicate_sql(self, quoted_column: str, max_digits: int, decimal_places: int) -> str:
        # The bound as a wide Decimal - one past the column's own digits overflows it.
        whole_digits_limit = CLICKHOUSE_WIDE_DECIMAL_BOUND_TEMPLATE.format("1" + "0" * (max_digits - decimal_places))
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
                return limit.get_overflow_predicate_sql(self, element_sql)
            return self.get_held_value_overflow_predicate_sql(held_path[1:], limit, element_sql)
        values_sql = {
            HeldValueStep.ELEMENT: quoted_column,
            HeldValueStep.KEY: f"mapKeys({quoted_column})",
            HeldValueStep.VALUE: f"mapValues({quoted_column})",
        }[step]
        # A name per depth - a lambda inside a lambda reads the value of its own depth.
        value_sql = f"hare_held_value_{len(held_path)}"
        if len(held_path) == 1:
            predicate_sql = limit.get_overflow_predicate_sql(self, value_sql)
        else:
            predicate_sql = self.get_held_value_overflow_predicate_sql(held_path[1:], limit, value_sql)
        return f"arrayExists({value_sql} -> {predicate_sql}, {values_sql})"
