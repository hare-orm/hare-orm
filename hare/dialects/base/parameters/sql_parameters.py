from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from hare.dialects.enums import ParameterPosition

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field
    from hare.sql.enums import JsonValueType


class SqlParameters:
    """How a dialect binds values: its placeholders, the casts a bare parameter needs, the sources
    a multi-row ``INSERT`` reads its rows from.

    Attributes:
        placeholder_template: A bound parameter's placeholder, ``{}`` standing for its index from 1.
        single_parameter_in_list_min_length: The length from which an ``__in``/``__not_in`` list
            binds as one parameter - a plan key holds no length for such a list. None for a dialect
            binding every value as a parameter of its own.
        describes_json_containment_by_shape: Whether the SQL of a JSON ``__contains``/``__contained_by``
            follows the compared value's keys and nesting - a plan key then holds them. False for a
            dialect binding the whole value as one parameter.
    """

    placeholder_template: str = "?"
    single_parameter_in_list_min_length: int | None = None
    describes_json_containment_by_shape: bool = False

    def __init__(self, dialect: Dialect) -> None:
        """
        Args:
            dialect: The dialect whose values are bound.
        """
        self.dialect = dialect

    def get_placeholder(self, index: int) -> str:
        """The placeholder of the ``index``-th (from 1) bound parameter."""
        return self.placeholder_template.format(index)

    @property
    def numbers_parameters(self) -> bool:
        """Whether a placeholder names its parameter's number (``$1``) - a value rendered twice
        then binds once, and the database sees both places as the same expression.

        Returns:
            True for numbered placeholders.
        """
        return "{}" in self.placeholder_template

    def get_bindable_number(self, value: int | float | Decimal) -> int | float | Decimal:
        """A number as bound where it stands for a JSON number.

        Args:
            value: The number.

        Returns:
            The value to bind.
        """
        return value

    def get_default_rows_source_sql(self, row_count: int) -> str | None:
        """The source an ``INSERT INTO table <source>`` writes ``row_count`` rows of column
        defaults from in one statement, None when each row needs a statement of its own.

        Args:
            row_count: How many rows.

        Returns:
            The SQL, or None.
        """
        return None

    def get_column_arrays_rows_source_sql(
        self, column_cast_types: Sequence[str], first_parameter_index: int
    ) -> str | None:
        """The source a multi-row INSERT writes its rows from when it binds one array parameter per
        column - the arrays read element by element into rows - or None when the dialect binds a
        parameter per value (``VALUES``).

        Args:
            column_cast_types: The SQL type of each column's values, in column order.
            first_parameter_index: The number of the first column's parameter.

        Returns:
            The SQL, or None.
        """
        return None

    def get_cast_parameter_sql(self, parameter_sql: str, value: Any) -> str:
        """A bare parameter nothing around it types (a ``CASE`` branch, a boolean), cast to the
        type of ``value`` where the dialect needs it.

        Args:
            parameter_sql: The parameter's placeholder.
            value: The bound value.

        Returns:
            The parameter SQL.
        """
        return parameter_sql

    def get_parameter_cast_type(self, value: Any, position: ParameterPosition) -> str | None:
        """The SQL type a bound literal is cast to where nothing around it types the parameter.

        Args:
            value: The value the literal binds as.
            position: Where the literal stands in the statement.

        Returns:
            The type, None when the database types the parameter itself - always, by default.
        """
        return None

    def get_json_object_value_cast_type(self, value: Any, value_type: JsonValueType) -> str | None:
        """The SQL type a bound literal written into a JSON object is cast to.

        Args:
            value: The value the literal binds as.
            value_type: How the value is written into the object.

        Returns:
            The type, None when the database types the parameter itself - always, by default.
        """
        return None

    def get_field_parameter_cast_type(self, field: Field[Any]) -> str | None:
        """The SQL type a parameter holding a value of a field's column is cast to where nothing
        around it types the parameter (the operand of ``IS NULL``, a ``VALUES`` row).

        Args:
            field: The field.

        Returns:
            The type, None when the database types the parameter itself - always, by default.
        """
        return None

    def get_copy_column_type(self, field: Field[Any]) -> str:
        """The type a bulk load (``DatabaseClient.copy()``) is told a field's column has.

        Args:
            field: The field.

        Returns:
            The column's type as the table declares it, by default.
        """
        return str(field.get_column_type(self.dialect))

    def supports_copy_column_type(self, column_type: str) -> bool:
        """Whether the bulk load loads a column type - ``bulk_create(use_copy=True)`` checks every
        column before loading any row.

        Args:
            column_type: The column type, as ``get_copy_column_type()`` names it.

        Returns:
            True when ``Features.supports_copy`` is set and the bulk load loads the type; False by
            default.
        """
        return False
