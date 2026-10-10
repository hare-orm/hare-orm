from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.dialects.clickhouse.types.clickhouse_nullable_types import ClickhouseNullableTypes
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.dialects.clickhouse.types.constants import CLICKHOUSE_UNSIGNED_LITERAL_FLOOR, CLICKHOUSE_UNSIGNED_TYPE_PREFIX
from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
from hare.exceptions import UnSupportedError
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.data.containers.container_field import ContainerField
from hare.fields.data.containers.declarations import MapValue, TupleValue
from hare.fields.data.containers.map_field import MapField
from hare.fields.data.containers.tuple_field import TupleField

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.field import Field
    from hare.models import Model


class ClickhouseContainerValues:
    """How ClickHouse stores containers - ``Array(T)``, ``Map(K, V)``, ``Tuple(T, ...)`` of the
    ClickHouse types of the held fields, to any depth. A held field with ``null=True`` is
    ``Nullable(T)``; a container itself is never ``Nullable`` - a NULL container is written as an
    empty one (an empty array, an empty map), and a tuple, which has no empty value, holds no NULL. An
    empty container is bound with its type, at any depth - its literal alone has none; so is a signed
    integer of an array, a map or a tuple read as a ``UInt64`` from its literal."""

    def __init__(self, types: TypeRegistry, dialect: Dialect) -> None:
        """
        Args:
            types: The ClickHouse types.
            dialect: The dialect the types are of.
        """
        self.types = types
        self.dialect = dialect

    def get_column_type(self, field: Field[Any]) -> str | None:
        """The container's column type - None when a held field has no column type on ClickHouse.

        Args:
            field: The container field.

        Returns:
            The column type.

        Raises:
            UnSupportedError: A tuple, top-level or held, is declared with ``null=True``.
        """
        if any(
            not child_field.exists_on(self.dialect) for child_field in cast("ContainerField", field).get_child_fields()
        ):
            return None
        if isinstance(field, TupleField) and field.null:
            raise UnSupportedError(
                f"TupleField {field.model_field_name or ''} holds no NULL on ClickHouse - a Tuple has no empty value"
            )
        if isinstance(field, ArrayField):
            return f"Array({self.get_held_type(field.base_field)})"
        if isinstance(field, MapField):
            return f"Map({self.get_held_type(field.key_field)}, {self.get_held_type(field.value_field)})"
        tuple_field = cast("TupleField", field)
        element_types = [self.get_held_type(child_field) for child_field in tuple_field.fields_in_order]
        if tuple_field.element_names is not None:
            element_types = [
                f"{name} {element_type}"
                for name, element_type in zip(tuple_field.element_names, element_types, strict=True)
            ]
        return f"Tuple({', '.join(element_types)})"

    def get_held_type(self, field: Field[Any]) -> str:
        """The type of a held value - ``Nullable(T)`` of a field with ``null=True`` that isn't a
        container.

        Args:
            field: The held field.

        Returns:
            The type.
        """
        column_type = field.get_column_type(self.dialect)
        return ClickhouseNullableTypes.get_nullable_type(column_type) if field.null else column_type

    @staticmethod
    def get_empty_value(field: ContainerField) -> Any:
        """The value a NULL container is written as.

        Args:
            field: The container field.

        Returns:
            An empty list or map.
        """
        return MapValue() if isinstance(field, MapField) else []

    def to_db(self, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The container as the server takes it - a NULL one empty.

        Args:
            field: The container field.
            value: The Python value.
            instance: The model (class) it is written or compared for.

        Returns:
            The value.
        """
        container_field = cast("ContainerField", field)
        db_value = container_field.get_dialect_db_value(value, instance, self.types)
        if db_value is None:
            db_value = self.get_empty_value(container_field)
        return self.get_typed_containers(container_field, db_value)

    @staticmethod
    def holds_typed_values(field: Field[Any]) -> bool:
        """Whether a container holds ``Dynamic`` or ``Variant`` values, at any depth.

        Args:
            field: The container field.

        Returns:
            Whether it does.
        """
        # Local import: the ClickHouse fields import the types, which import this module.
        from hare.dialects.clickhouse.fields.dynamic_field import DynamicField
        from hare.dialects.clickhouse.fields.variant_field import VariantField

        return any(
            isinstance(child_field, DynamicField | VariantField)
            or (isinstance(child_field, ContainerField) and ClickhouseContainerValues.holds_typed_values(child_field))
            for child_field in cast("ContainerField", field).get_child_fields()
        )

    def get_typed_containers(self, field: ContainerField, value: Any) -> Any:
        """A container with each empty container in it bound with its type - ClickHouse writes an
        empty array as one of no type, which a ``SELECT`` casts to no array of arrays (``[[]]`` to an
        array of three dimensions) and to no array of ``Dynamic``.

        Args:
            field: The container field.
            value: The value, as the server takes it.

        Returns:
            The value.
        """
        if isinstance(value, list | dict) and not value:
            return ClickhouseTypedValue(
                value, ClickhouseTypeNames.get_server_type(field.get_column_type(self.dialect))
            )
        if isinstance(field, ArrayField):
            if not isinstance(field.base_field, ContainerField):
                return self.get_typed_integers(field.base_field, value)
            return [
                element if element is None else self.get_typed_containers(field.base_field, element)
                for element in value
            ]
        if isinstance(field, MapField):
            given_keys = list(value)
            keys = self.get_typed_integers(field.key_field, given_keys)
            if not isinstance(field.value_field, ContainerField):
                given_items = list(value.values())
                items = self.get_typed_integers(field.value_field, given_items)
                if keys is given_keys and items is given_items:
                    return value
                return MapValue(zip(keys, items, strict=True))
            return MapValue(
                (key, item if item is None else self.get_typed_containers(field.value_field, item))
                for key, item in zip(keys, value.values(), strict=True)
            )
        tuple_field = cast("TupleField", field)
        floor = CLICKHOUSE_UNSIGNED_LITERAL_FLOOR
        return TupleValue(
            self.get_typed_integers(element_field, [element])[0]
            if type(element) is int and element >= floor
            else element
            if element is None or not isinstance(element_field, ContainerField)
            else self.get_typed_containers(element_field, element)
            for element_field, element in zip(tuple_field.fields_in_order, value, strict=True)
        )

    def get_typed_integers(self, field: Field[Any], values: list[Any]) -> list[Any]:
        """The values of an array or a map with each integer a literal reads as a ``UInt64`` bound with
        its signed field's type - beside a negative one the server finds no type of both.

        Args:
            field: The field of the values.
            values: The values.

        Returns:
            The values - the same list when none is bound.
        """
        floor = CLICKHOUSE_UNSIGNED_LITERAL_FLOOR
        if not any(type(value) is int and value >= floor for value in values):
            return values
        held_type = ClickhouseTypeNames.get_server_type(self.get_held_type(field))
        if held_type.removeprefix("Nullable(").startswith(CLICKHOUSE_UNSIGNED_TYPE_PREFIX):
            return values
        return [
            ClickhouseTypedValue(value, held_type) if type(value) is int and value >= floor else value
            for value in values
        ]

    def to_python(self, field: Field[Any], value: Any) -> Any:
        """The Python value of a container the driver returned.

        Args:
            field: The container field.
            value: The value.

        Returns:
            The value.
        """
        return cast("ContainerField", field).get_dialect_python_value(value, self.types)
