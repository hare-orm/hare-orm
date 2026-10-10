from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
from hare.exceptions import ConfigurationError, UnSupportedError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.dialects.clickhouse.fields.variant_field import VariantField
    from hare.fields.field import Field
    from hare.models import Model


class ClickhouseVariantValues:
    """How ClickHouse writes and reads a ``Variant(T, ...)`` column - its fields' types, each named as
    the server names it, in the server's order. A value is written with the type of the field taking
    it; a value read is given to the field whose type the drivers read as the value's Python type -
    two fields read as one Python type are refused."""

    def __init__(self, types: TypeRegistry, dialect: Dialect) -> None:
        """
        Args:
            types: The ClickHouse types.
            dialect: The dialect the types are of.
        """
        self.types = types
        self.dialect = dialect

    def get_column_type(self, field: Field[Any]) -> str | None:
        """``Variant(T, ...)`` of the fields' types, sorted as the server sorts them - None when a field
        has no column type on ClickHouse.

        Args:
            field: The variant field.

        Returns:
            The column type.

        Raises:
            ConfigurationError: Two fields are of one type, or the drivers read their values as one
                Python type.
            UnSupportedError: A field holds NULL - a variant holds NULL itself.
        """
        variant_field = cast("VariantField", field)
        if any(not held_field.exists_on(self.dialect) for held_field in variant_field.variant_fields):
            return None
        if any(held_field.null for held_field in variant_field.variant_fields):
            raise UnSupportedError(
                f"VariantField {field.model_field_name or ''}: its fields hold no NULL on ClickHouse - "
                "give the VariantField null=True instead"
            )
        server_types = variant_field.server_types
        fields_by_python_type: dict[type, str] = {}
        for server_type, python_types in zip(server_types, variant_field.read_python_types, strict=True):
            for python_type in python_types:
                if python_type in fields_by_python_type:
                    raise ConfigurationError(
                        f"VariantField {field.model_field_name or ''}: the values of "
                        f"{fields_by_python_type[python_type]} and {server_type} are read as one Python type - a "
                        "value read is told by its Python type"
                    )
                fields_by_python_type[python_type] = server_type
        return f"Variant({', '.join(sorted(server_types))})"

    def to_db(self, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The value as the field taking it writes it, bound with that field's type.

        Args:
            field: The variant field.
            value: The Python value.
            instance: The model (class) it is written or compared for.

        Returns:
            The typed value, written into the column's type.

        Raises:
            ValidationError: The value is None on a field holding no NULL, or no field takes it.
        """
        held_type = self.get_column_type(field)
        if value is None:
            field.validate(value)
            return ClickhouseTypedValue(None, "Nothing", held_type)
        typed_value = self.to_lookup(field, value, instance)
        return ClickhouseTypedValue(typed_value.value, typed_value.column_type, held_type)

    def to_lookup(self, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The value a filter compares, as the field taking it writes it, bound with that field's type.

        Args:
            field: The variant field.
            value: The Python value.
            instance: The model (class) the filter is built for.

        Returns:
            The typed value; None for None.

        Raises:
            ValidationError: No field takes the value.
        """
        if value is None:
            return None
        variant_field = cast("VariantField", field)
        index = variant_field.get_variant_index(value)
        db_value = self.types.get_db_value(variant_field.variant_fields[index], value, instance)
        return ClickhouseTypedValue(db_value, variant_field.server_types[index])

    def to_python(self, field: Field[Any], value: Any) -> Any:
        """The Python value of the field whose type the drivers read as the value's Python type.

        Args:
            field: The variant field.
            value: The value the driver returned.

        Returns:
            The value.
        """
        if value is None:
            return None
        variant_field = cast("VariantField", field)
        value_type = type(value)
        for held_field, read_python_types in zip(
            variant_field.variant_fields, variant_field.read_python_types, strict=True
        ):
            if value_type in read_python_types:
                return self.types.get_python_value(held_field, value)
        return value
