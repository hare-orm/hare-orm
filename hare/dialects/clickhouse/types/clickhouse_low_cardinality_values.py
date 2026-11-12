from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hare.dialects.base.types.type_registry import TypeRegistry
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.dialects.clickhouse.types.constants import CLICKHOUSE_LOW_CARDINALITY_TYPE_NAMES
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field


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

        Raises:
            ConfigurationError: The base field's type is one ClickHouse keeps no ``LowCardinality``
                of - only of text, ``FixedString`` and UUIDs.
        """
        base_type = field.base_field.get_column_type(self.dialect)  # type: ignore[attr-defined]
        type_name, __ = ClickhouseTypeNames.get_type_parts(
            ClickhouseTypeNames.get_type_without_nullable(ClickhouseTypeNames.get_server_type(base_type))
        )
        if type_name not in CLICKHOUSE_LOW_CARDINALITY_TYPE_NAMES:
            raise ConfigurationError(
                f"LowCardinalityField {field.model_field_name or ''} holds {base_type} - ClickHouse keeps "
                "LowCardinality of text, FixedString and UUID values only"
            )
        return f"LowCardinality({base_type})"

    def prepare_db(self, field: Field[Any]) -> Callable[[Any, Any], Any]:
        """Bind the inner writer while retaining the outer field's NULL validation."""
        write = TypeRegistry.get_db_writer(self.types, field.base_field)  # type: ignore[attr-defined]

        def write_value(value: Any, instance: Any) -> Any:
            if value is None:
                field.validate(value)
                return None
            return write(value, instance)

        return write_value

    def prepare_lookup(self, field: Field[Any]) -> Callable[[Any, Any], Any]:
        """Comparisons retain the base field's lookup contract, including NULL."""
        return TypeRegistry.get_lookup_writer(self.types, field.base_field)  # type: ignore[attr-defined]

    def prepare_python(self, field: Field[Any]) -> Callable[[Any], Any]:
        """Bind the inner reader without passing an outer NULL through it."""
        read = TypeRegistry.get_python_reader(self.types, field.base_field)  # type: ignore[attr-defined]
        return lambda value: None if value is None else read(value)
