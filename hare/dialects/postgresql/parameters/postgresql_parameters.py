from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.dialects.base.parameters.sql_parameters import SqlParameters
from hare.dialects.enums import ParameterPosition
from hare.dialects.postgresql.parameters.constants import POSTGRES_IN_ARRAY_THRESHOLD

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.sql.enums import JsonValueType


class PostgresqlParameters(SqlParameters):
    """PostgreSQL's parameters - numbered ``$n`` placeholders, typed by a cast where nothing around
    them types them, multi-row sources from ``generate_series()`` and ``unnest()``."""

    placeholder_template = "${}"
    single_parameter_in_list_min_length = POSTGRES_IN_ARRAY_THRESHOLD

    def get_default_rows_source_sql(self, row_count: int) -> str | None:
        return f"SELECT FROM generate_series(1, {int(row_count)})"  # nosec B608 - an int, not input

    def get_column_arrays_rows_source_sql(
        self, column_cast_types: Sequence[str], first_parameter_index: int
    ) -> str | None:
        # unnest() reads a nested array element by element - an array column stays on VALUES.
        if any(cast_type.rstrip().endswith("]") for cast_type in column_cast_types):
            return None
        arguments = ",".join(
            f"{self.get_placeholder(first_parameter_index + index)}::{cast_type}[]"
            for index, cast_type in enumerate(column_cast_types)
        )
        return f"SELECT * FROM unnest({arguments})"  # nosec B608 - placeholders and column types

    def get_parameter_cast_type(self, value: Any, position: ParameterPosition) -> str | None:
        # PostgreSQL types a parameter only from what's around it: a bare one is text, which the
        # driver then refuses to bind a real value against, or has no type at all.
        # Local import: the constants module instantiates the dialect, which imports this one.
        from hare.dialects.postgresql.constants import POSTGRESQL_PARAMETER_TYPES_BY_POSITION
        from hare.dialects.postgresql.parameters.constants import (
            POSTGRESQL_SELECTED_LITERAL_POSITIONS,
            POSTGRESQL_SELECTED_LITERAL_TYPES,
        )

        if position in POSTGRESQL_SELECTED_LITERAL_POSITIONS:
            for value_type, type_name in POSTGRESQL_SELECTED_LITERAL_TYPES:
                if isinstance(value, value_type):
                    return type_name
        return POSTGRESQL_PARAMETER_TYPES_BY_POSITION[position].get(type(value))

    def get_json_object_value_cast_type(self, value: Any, value_type: JsonValueType) -> str | None:
        # Local imports: the constants module instantiates the dialect, which imports this one;
        # hare.sql renders through the dialect.
        from hare.dialects.postgresql.parameters.constants import POSTGRESQL_JSON_OBJECT_VALUE_TYPES
        from hare.sql.enums import JsonValueType

        if value is None or value_type in {JsonValueType.JSON, JsonValueType.BINARY}:
            return POSTGRESQL_JSON_OBJECT_VALUE_TYPES[value_type]
        return self.get_parameter_cast_type(value, ParameterPosition.FUNCTION_ARGUMENT)

    def get_field_parameter_cast_type(self, field: Field[Any]) -> str | None:
        return field.get_column_type(self.dialect)

    def get_cast_parameter_sql(self, parameter_sql: str, value: Any) -> str:
        # PostgreSQL types a parameter only from what's around it - a CASE of bare parameters
        # would otherwise be text, which the driver then refuses to bind a real value against.
        # Local import: the constants module instantiates the dialect, which imports this one.
        from hare.dialects.postgresql.parameters.constants import POSTGRESQL_PARAMETER_CASTS

        for value_type, type_name in POSTGRESQL_PARAMETER_CASTS:
            if isinstance(value, value_type):
                return f"{parameter_sql}::{type_name}"
        return parameter_sql

    def get_copy_column_type(self, field: Field[Any]) -> str:
        # Binary COPY is told each type by its name alone ("VARCHAR(255)" -> "VARCHAR").
        return super().get_copy_column_type(field).split("(")[0].strip()

    def supports_copy_column_type(self, column_type: str) -> bool:
        # The types both drivers' binary COPY encodes - not an array, a range or an extension's type.
        # Local import: the constants module instantiates the dialect, which imports this one.
        from hare.dialects.postgresql.parameters.constants import COPY_SUPPORTED_SQL_TYPES

        return column_type.upper() in COPY_SUPPORTED_SQL_TYPES
