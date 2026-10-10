from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.dialects.clickhouse.fields.low_cardinality_field import LowCardinalityField
    from hare.fields.field import Field
    from hare.models import Model


class ClickhouseLowCardinalityValues:
    """How ClickHouse writes and reads a ``LowCardinality(T)`` column - as its base field's column,
    through the ClickHouse types; NULL where the wrapping field holds it."""

    def __init__(self, types: TypeRegistry, dialect: Dialect) -> None:
        """
        Args:
            types: The ClickHouse types.
            dialect: The dialect the types are of.
        """
        self.types = types
        self.dialect = dialect

    def get_column_type(self, field: Field[Any]) -> str:
        """``LowCardinality(T)`` of the base field's type.

        Args:
            field: The low cardinality field.

        Returns:
            The column type.
        """
        return f"LowCardinality({cast('LowCardinalityField', field).base_field.get_column_type(self.dialect)})"

    def to_db(self, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The value as the base field's column binds it.

        Args:
            field: The low cardinality field.
            value: The Python value.
            instance: The model (class) it is written or compared for.

        Returns:
            The value - None for None, refused where the field holds no NULL.
        """
        if value is None:
            field.validate(value)
            return None
        return self.types.get_db_value(cast("LowCardinalityField", field).base_field, value, instance)

    def to_lookup(self, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The value a filter compares, as the base field's column binds it.

        Args:
            field: The low cardinality field.
            value: The Python value.
            instance: The model (class) the filter is built for.

        Returns:
            The value.
        """
        return self.types.get_lookup_value(cast("LowCardinalityField", field).base_field, value, instance)

    def to_python(self, field: Field[Any], value: Any) -> Any:
        """The Python value the base field reads.

        Args:
            field: The low cardinality field.
            value: The value the driver returned.

        Returns:
            The value.
        """
        if value is None:
            return None
        return self.types.get_python_value(cast("LowCardinalityField", field).base_field, value)
