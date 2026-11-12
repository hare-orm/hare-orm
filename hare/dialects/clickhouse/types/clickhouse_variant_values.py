from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hare.dialects.base.types.type_registry import TypeRegistry
from hare.dialects.clickhouse.enums import ClickhouseTypeName
from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
from hare.exceptions import ConfigurationError, UnSupportedError

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.clickhouse.fields.variant_field import VariantField
    from hare.fields.field import Field


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
        variant_field: VariantField = field  # type: ignore[assignment]
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

    def prepare_db(self, field: Field[Any]) -> Callable[[Any, Any], Any]:
        """Validate the declared alternatives once and retain their storage conversion."""
        held_type = self.get_column_type(field)
        write = self.prepare_lookup(field)

        def write_value(value: Any, instance: Any) -> Any:
            if value is None:
                field.validate(value)
                return ClickhouseTypedValue(None, ClickhouseTypeName.NOTHING, held_type)
            typed_value = write(value, instance)
            return ClickhouseTypedValue(typed_value.value, typed_value.column_type, held_type)

        return write_value

    def prepare_lookup(self, field: Field[Any]) -> Callable[[Any, Any], Any]:
        """Prepare alternative writers; choosing an alternative still depends on each value."""
        variant_field: VariantField = field  # type: ignore[assignment]
        writers = tuple(TypeRegistry.get_db_writer(self.types, child) for child in variant_field.variant_fields)
        server_types = variant_field.server_types

        def write_value(value: Any, instance: Any) -> Any:
            if value is None:
                return None
            index = variant_field.get_variant_index(value)
            return ClickhouseTypedValue(writers[index](value, instance), server_types[index])

        return write_value

    def prepare_python(self, field: Field[Any]) -> Callable[[Any], Any]:
        """Prepare read alternatives, retaining first-match and unknown-value behavior."""
        variant_field: VariantField = field  # type: ignore[assignment]
        readers: dict[type, Callable[[Any], Any]] = {}
        for child, python_types in zip(variant_field.variant_fields, variant_field.read_python_types, strict=True):
            read = TypeRegistry.get_python_reader(self.types, child)
            for python_type in python_types:
                readers.setdefault(python_type, read)

        def read_value(value: Any) -> Any:
            if value is None:
                return None
            read = readers.get(type(value))
            return value if read is None else read(value)

        return read_value
