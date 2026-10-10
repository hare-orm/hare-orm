from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import UnSupportedError
from hare.fields.data.containers.container_field import ContainerField
from hare.fields.enums import NarrowedValueSource
from hare.fields.field import Field
from hare.fields.narrowing.decimal_digits_limit import DecimalDigitsLimit
from hare.fields.narrowing.enum_values_limit import EnumValuesLimit
from hare.fields.narrowing.integer_range_limit import IntegerRangeLimit
from hare.fields.narrowing.narrowing_limit import NarrowingLimit
from hare.fields.narrowing.text_length_limit import TextLengthLimit
from hare.migrations.exceptions import FieldNarrowingDataLossError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.data.containers.container_field import HeldValuePath


class ColumnNarrowingCheck(SchemaEditorPart):
    """The check that narrowing a column - a shorter text, a smaller integer or decimal - loses no
    stored value, made before the column is altered."""

    __slots__ = ()

    async def check_alter_field_narrowing_data_loss(
        self, qualified_table: str, old_field: Field[Any], new_field: Field[Any], quoted_column: str
    ) -> None:
        """Raise if an existing row doesn't fit ``new_field`` - a value the type change would
        silently truncate or round, or one the narrower column would hold beyond its declared size.

        Args:
            qualified_table: The already schema-qualified, quoted table name.
            old_field: The field's previous definition.
            new_field: The field's new, narrower definition.
            quoted_column: The already-quoted column name to check.

        Raises:
            FieldNarrowingDataLossError: At least one existing row would overflow the new type.
        """
        limit = new_field.get_narrowing_limit(old_field)
        overflow_predicates = (
            [] if limit is None else [self.get_narrowing_overflow_predicate_sql(limit, quoted_column)]
        )
        if isinstance(new_field, ContainerField):
            overflow_predicates.extend(
                self.get_held_value_overflow_predicate_sql(held_path, held_limit, quoted_column)
                for held_path, held_limit in new_field.get_held_narrowing_limits(old_field)
            )
        if not overflow_predicates:
            return
        overflow_predicate = " OR ".join(f"({predicate})" for predicate in overflow_predicates)
        rows = await self.editor.client.execute_dicts(
            f"SELECT count(*) AS overflow_count FROM {qualified_table} WHERE {overflow_predicate}"  # nosec B608
        )
        overflow_count = rows[0]["overflow_count"]
        if overflow_count:
            raise FieldNarrowingDataLossError(
                f"Cannot narrow column {quoted_column} on {qualified_table} to the new definition "
                f"of field '{new_field.model_field_name}' - {overflow_count} existing row(s) would "
                "not fit the new column type. Clean up or widen the offending "
                "values by hand first, then retry the migration."
            )

    def get_narrowing_overflow_predicate_sql(self, limit: NarrowingLimit, quoted_column: str) -> str:
        """A WHERE predicate matching the rows whose value is beyond a narrowing limit.

        Args:
            limit: The limit.
            quoted_column: The quoted column.

        Returns:
            The predicate.
        """
        if isinstance(limit, TextLengthLimit):
            return self.get_text_overflow_predicate_sql(limit, quoted_column)
        if isinstance(limit, DecimalDigitsLimit):
            return self.get_decimal_overflow_predicate_sql(quoted_column, limit.max_digits, limit.decimal_places)
        if isinstance(limit, EnumValuesLimit):
            return self.get_enum_overflow_predicate_sql(limit, quoted_column)
        return self.get_integer_overflow_predicate_sql(limit, quoted_column)

    def get_text_overflow_predicate_sql(self, limit: TextLengthLimit, quoted_column: str) -> str:
        """A WHERE predicate matching the rows whose value is longer as text than a limit.

        Args:
            limit: The limit.
            quoted_column: The quoted column.

        Returns:
            The predicate.
        """
        if limit.source == NarrowedValueSource.TEXT:
            text_sql = quoted_column
        elif limit.source == NarrowedValueSource.BOOLEAN:
            text_sql = f"CASE WHEN {quoted_column} THEN 'true' ELSE 'false' END"
        else:
            text_sql = f"CAST({quoted_column} AS TEXT)"
        return f"length({text_sql}) > {limit.max_length}"

    def get_integer_overflow_predicate_sql(self, limit: IntegerRangeLimit, quoted_column: str) -> str:
        """A WHERE predicate matching the rows whose number is outside an integer's range - or, where
        the old column held fractions, isn't whole.

        Args:
            limit: The limit.
            quoted_column: The quoted column.

        Returns:
            The predicate.
        """
        if not limit.checks_fraction:
            return f"{quoted_column} < {limit.lowest} OR {quoted_column} > {limit.highest}"
        whole_digits = len(str(max(abs(limit.lowest), abs(limit.highest))))
        number_sql = f"CAST({quoted_column} AS NUMERIC)"
        return (
            f"{self.get_decimal_overflow_predicate_sql(quoted_column, whole_digits, 0)} "
            f"OR {number_sql} < {limit.lowest} OR {number_sql} > {limit.highest}"
        )

    def get_enum_overflow_predicate_sql(self, limit: EnumValuesLimit, quoted_column: str) -> str:
        """A WHERE predicate matching the rows holding a value an enum no longer has.

        Args:
            limit: The enum's values.
            quoted_column: The quoted column.

        Returns:
            The predicate.
        """
        literals = self.editor.client.dialect.literals
        values_sql = ", ".join(literals.get_literal_sql(value) for value in sorted(limit.values))
        return f"{quoted_column} NOT IN ({values_sql})"

    def get_held_value_overflow_predicate_sql(
        self, held_path: HeldValuePath, limit: NarrowingLimit, quoted_column: str
    ) -> str:
        """A WHERE predicate matching the rows holding a value beyond a narrowing limit in a container
        column, at any depth - a dialect with containers reads their values its own way.

        Args:
            held_path: The way from the column to the values.
            limit: Their limit.
            quoted_column: The quoted column.

        Returns:
            The predicate.

        Raises:
            UnSupportedError: The dialect reads no value of a container - by default.
        """
        raise UnSupportedError(f"The {self.editor.client.dialect} dialect checks the narrowing of no container")

    def get_decimal_overflow_predicate_sql(self, quoted_column: str, max_digits: int, decimal_places: int) -> str:
        """A WHERE predicate matching rows whose numeric value doesn't fit
        ``DECIMAL(max_digits, decimal_places)`` - more fractional digits, or too many whole ones.

        Args:
            quoted_column: The quoted column holding an integer, float or decimal.
            max_digits: The digits the type holds.
            decimal_places: How many of them are fractional.

        Returns:
            The predicate.
        """
        number_sql = f"CAST({quoted_column} AS NUMERIC)"
        whole_digits_limit = "1" + "0" * (max_digits - decimal_places)
        return f"round({number_sql}, {decimal_places}) <> {number_sql} OR abs({number_sql}) >= {whole_digits_limit}"
