from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck
from hare.dialects.postgresql.schema.constants import POSTGRESQL_HELD_VALUE_ALIAS
from hare.exceptions import UnSupportedError
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.enums import HeldValueStep
from hare.fields.generated_field import GeneratedField
from hare.fields.narrowing.constants import FLOAT32_MANTISSA_BITS
from hare.fields.narrowing.float_precision_limit import FloatPrecisionLimit
from hare.migrations.exceptions import FieldNarrowingDataLossError

if TYPE_CHECKING:
    from hare.fields.data.containers.container_field import HeldValuePath
    from hare.fields.field import Field
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit


class PostgresqlColumnNarrowingCheck(ColumnNarrowingCheck):
    """The narrowing check of PostgreSQL - the values of an array column read by ``unnest()``, which reads
    the elements of every dimension."""

    __slots__ = ()

    async def check_alter_field_narrowing_data_loss(
        self, qualified_table: str, old_field: Field[Any], new_field: Field[Any], quoted_column: str
    ) -> None:
        await super().check_alter_field_narrowing_data_loss(qualified_table, old_field, new_field, quoted_column)
        dimensions = PostgresqlColumnNarrowingCheck.get_array_dimensions(new_field)
        if not dimensions or dimensions == PostgresqlColumnNarrowingCheck.get_array_dimensions(old_field):
            return
        # An array column is of one type whatever its dimensions - a row keeps the ones it was written with,
        # which the field would read no more.
        rows = await self.editor.client.execute_dicts(
            f"SELECT count(*) AS other_count FROM {qualified_table} "  # nosec B608
            f"WHERE array_ndims({quoted_column}) <> {dimensions}"
        )
        other_count = rows[0]["other_count"]
        if other_count:
            raise FieldNarrowingDataLossError(
                f"Cannot change column {quoted_column} on {qualified_table} to the new definition of field "
                f"{new_field.model_field_name!r} - {other_count} existing row(s) hold an array of other dimensions "
                f"than its {dimensions}. Rewrite the offending values by hand first, then retry the migration."
            )

    @staticmethod
    def get_array_dimensions(field: Field[Any]) -> int:
        """The dimensions of an array field's values - an array of arrays has two.

        Args:
            field: The field.

        Returns:
            The dimensions, 0 for a field of no array.
        """
        dimensions = 0
        field = GeneratedField.get_effective_field(field)
        while isinstance(field, ArrayField):
            dimensions += 1
            field = field.base_field
        return dimensions

    def get_network_overflow_predicate_sql(self, quoted_column: str) -> str:
        """A WHERE predicate matching the rows whose address has host bits set beyond its prefix.

        Args:
            quoted_column: The quoted column.

        Returns:
            The predicate.
        """
        return f"{quoted_column} <> network({quoted_column})"

    def get_float_overflow_predicate_sql(self, limit: FloatPrecisionLimit, quoted_column: str) -> str:
        # A float's numeric keeps 15 digits - its text is the shortest reading back as the float.
        float_type = "DOUBLE PRECISION" if limit.mantissa_bits > FLOAT32_MANTISSA_BITS else "REAL"
        float_text_sql = f"CAST(CAST({quoted_column} AS {float_type}) AS TEXT)"
        return f"CAST({float_text_sql} AS NUMERIC) <> CAST({quoted_column} AS NUMERIC)"

    def get_held_value_overflow_predicate_sql(
        self, held_path: HeldValuePath, limit: NarrowingLimit, quoted_column: str
    ) -> str:
        """A WHERE predicate matching the rows holding a value beyond a narrowing limit in an array
        column, at any depth.

        Args:
            held_path: The way from the column to the values.
            limit: Their limit.
            quoted_column: The quoted column.

        Returns:
            The predicate.

        Raises:
            UnSupportedError: The way crosses something but arrays - a map or a tuple.
        """
        if any(step != HeldValueStep.ELEMENT for step, _position in held_path):
            raise UnSupportedError(f"The {self.editor.client.dialect} dialect checks the narrowing of arrays alone")
        element_sql = POSTGRESQL_HELD_VALUE_ALIAS
        predicate_sql = limit.get_overflow_predicate_sql(self, element_sql)
        return f"EXISTS (SELECT 1 FROM unnest({quoted_column}) AS {element_sql} WHERE {predicate_sql})"  # nosec B608
