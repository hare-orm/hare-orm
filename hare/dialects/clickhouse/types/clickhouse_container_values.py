from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hare.dialects.clickhouse.lookups.clickhouse_array_elements_key import ClickhouseArrayElementsKey
from hare.dialects.clickhouse.lookups.constants import (
    CLICKHOUSE_ARRAY_ELEMENT_VARIABLE,
    CLICKHOUSE_ARRAY_ZIP_FUNCTION_NAME,
    CLICKHOUSE_TUPLE_ELEMENT_FUNCTION_NAME,
    CLICKHOUSE_TUPLE_FUNCTION_NAME,
)
from hare.dialects.clickhouse.types.clickhouse_nullable_types import ClickhouseNullableTypes
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.dialects.clickhouse.types.constants import CLICKHOUSE_UNSIGNED_LITERAL_FLOOR, CLICKHOUSE_UNSIGNED_TYPE_PREFIX
from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
from hare.exceptions import UnSupportedError, ValidationError
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.data.containers.container_field import ContainerField
from hare.fields.data.containers.declarations import MapValue, TupleValue
from hare.fields.data.containers.map_field import MapField
from hare.fields.data.containers.tuple_field import TupleField
from hare.sql.terms.containers.declarations import MapKeysTerm, MapValuesTerm
from hare.sql.terms.functions.function import Function
from hare.sql.terms.values.literal_value import LiteralValue

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.field import Field
    from hare.sql.terms.term import Term


class ClickhouseContainerValues:
    """How ClickHouse stores containers - ``Array(T)``, ``Map(K, V)``, ``Tuple(T, ...)`` of the
    ClickHouse types of the held fields, to any depth. A held field with ``null=True`` is
    ``Nullable(T)`` when that held type supports NULL. A container itself is never ``Nullable``:
    nullable declarations and None values are refused for arrays, maps and tuples. An
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
            UnSupportedError: A container, top-level or held, is declared with ``null=True`` - an
                ``Array``, a ``Map`` and a ``Tuple`` of ClickHouse hold no NULL.
        """
        if any(not child_field.exists_on(self.dialect) for child_field in field.get_child_fields()):  # type: ignore[attr-defined]
            return None
        if field.null:
            raise UnSupportedError(
                f"{type(field).__name__} {field.model_field_name or ''} holds no NULL on ClickHouse - its "
                "Array, Map and Tuple hold none"
            )
        if isinstance(field, ArrayField):
            return f"Array({self.get_held_type(field.base_field)})"
        if isinstance(field, MapField):
            return f"Map({self.get_held_type(field.key_field)}, {self.get_held_type(field.value_field)})"
        tuple_field: TupleField = field  # type: ignore[assignment]
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

    def get_ordering_key(self, field: Field[Any], term: Term) -> Term | None:
        """What a container column orders by - a key holding each ``CharEnumField`` value by its label,
        which an ``Enum`` column orders otherwise; a map as the array of its key and value pairs.

        Args:
            field: The container field.
            term: The column.

        Returns:
            The key, None for the column itself.
        """
        return self.get_held_ordering_key(field, term, 0)

    def get_held_ordering_key(self, field: Field[Any], term: Term, depth: int) -> Term | None:
        """The key a held value orders by.

        Args:
            field: The field of the value.
            term: The value.
            depth: How many arrays hold it - naming the lambda variable of its elements.

        Returns:
            The key, None for the value itself.
        """
        if isinstance(field, ArrayField):
            variable = LiteralValue(CLICKHOUSE_ARRAY_ELEMENT_VARIABLE.format(depth=depth))
            element_key = self.get_held_ordering_key(field.base_field, variable, depth + 1)
            return None if element_key is None else ClickhouseArrayElementsKey(term, variable, element_key)
        if isinstance(field, TupleField):
            elements = [
                Function(CLICKHOUSE_TUPLE_ELEMENT_FUNCTION_NAME, term, LiteralValue(str(position)))
                for position in range(1, len(field.fields_in_order) + 1)
            ]
            element_keys = [
                self.get_held_ordering_key(child_field, element, depth)
                for child_field, element in zip(field.fields_in_order, elements, strict=True)
            ]
            if all(element_key is None for element_key in element_keys):
                return None
            return Function(
                CLICKHOUSE_TUPLE_FUNCTION_NAME,
                *(
                    element if element_key is None else element_key
                    for element, element_key in zip(elements, element_keys, strict=True)
                ),
            )
        if isinstance(field, MapField):
            # The pairs in the map's order, each keyed as a tuple of its key and value - the map's own
            # comparison, an enum by its label.
            variable = LiteralValue(CLICKHOUSE_ARRAY_ELEMENT_VARIABLE.format(depth=depth))
            pair_elements = [
                Function(CLICKHOUSE_TUPLE_ELEMENT_FUNCTION_NAME, variable, LiteralValue(str(position)))
                for position in (1, 2)
            ]
            pair_keys = [
                self.get_held_ordering_key(child_field, element, depth + 1)
                for child_field, element in zip((field.key_field, field.value_field), pair_elements, strict=True)
            ]
            if all(pair_key is None for pair_key in pair_keys):
                return None
            pairs = Function(CLICKHOUSE_ARRAY_ZIP_FUNCTION_NAME, MapKeysTerm(term), MapValuesTerm(term))
            pair_key = Function(
                CLICKHOUSE_TUPLE_FUNCTION_NAME,
                *(
                    element if element_key is None else element_key
                    for element, element_key in zip(pair_elements, pair_keys, strict=True)
                ),
            )
            return ClickhouseArrayElementsKey(pairs, variable, pair_key)
        if isinstance(field, ContainerField):
            return None
        function_cast = self.types.get_function_cast(field)
        return None if function_cast is None else function_cast(field, term)

    def prepare_db(self, field: Field[Any]) -> Callable[[Any, Any], Any]:
        """Prepare field traversal and typed literals independently of row values."""
        container_field: ContainerField = field  # type: ignore[assignment]
        write = container_field.get_dialect_db_writer(self.types)
        decorate = self.prepare_typed_containers(container_field)

        def write_value(value: Any, instance: Any) -> Any:
            db_value = write(value, instance)
            if db_value is None:
                raise ValidationError(f"{field.model_field_name}: a ClickHouse container holds no NULL, got None")
            return decorate(db_value)

        return write_value

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
            isinstance(child_field, (DynamicField, VariantField))
            or (isinstance(child_field, ContainerField) and ClickhouseContainerValues.holds_typed_values(child_field))
            for child_field in field.get_child_fields()  # type: ignore[attr-defined]
        )

    def prepare_typed_containers(self, field: ContainerField) -> Callable[[Any], Any]:
        """Prepare nested literal types once; empty values still receive their exact type."""
        empty_type = ClickhouseTypeNames.get_server_type(field.get_column_type(self.dialect))
        decorate: Callable[[Any], Any]
        if isinstance(field, ArrayField):
            if isinstance(field.base_field, ContainerField):
                child = self.prepare_typed_containers(field.base_field)

                def write_array(value: Any) -> Any:
                    return [None if element is None else child(element) for element in value]

                decorate = write_array
            else:
                decorate = self.prepare_typed_integers(field.base_field)
        elif isinstance(field, MapField):
            write_keys = self.prepare_typed_integers(field.key_field)
            if isinstance(field.value_field, ContainerField):
                write_item = self.prepare_typed_containers(field.value_field)

                def write_nested_map(value: Any) -> Any:
                    keys = write_keys(list(value))
                    return MapValue(
                        (key, None if item is None else write_item(item))
                        for key, item in zip(keys, value.values(), strict=True)
                    )

                decorate = write_nested_map
            else:
                write_items = self.prepare_typed_integers(field.value_field)

                def write_map(value: Any) -> Any:
                    given_keys, given_items = list(value), list(value.values())
                    keys, items = write_keys(given_keys), write_items(given_items)
                    if keys is given_keys and items is given_items:
                        return value
                    return MapValue(zip(keys, items, strict=True))

                decorate = write_map
        else:
            tuple_field: TupleField = field  # type: ignore[assignment]
            # Each position keeps separate handling of large integers and nested containers.
            positions = tuple(
                (
                    self.prepare_typed_integers(child_field),
                    self.prepare_typed_containers(child_field) if isinstance(child_field, ContainerField) else None,
                )
                for child_field in tuple_field.fields_in_order
            )
            floor = CLICKHOUSE_UNSIGNED_LITERAL_FLOOR

            def write_tuple(value: Any) -> Any:
                return TupleValue(
                    integers([element])[0]
                    if type(element) is int and element >= floor
                    else element
                    if element is None or nested is None
                    else nested(element)
                    for (integers, nested), element in zip(positions, value, strict=True)
                )

            decorate = write_tuple

        return lambda value: (
            ClickhouseTypedValue(value, empty_type)
            if isinstance(value, (list, dict)) and not value
            else decorate(value)
        )

    def prepare_typed_integers(self, field: Field[Any]) -> Callable[[list[Any]], list[Any]]:
        """Bind the declared signed type, retaining unchanged lists without large integers."""
        held_type = ClickhouseTypeNames.get_server_type(self.get_held_type(field))
        if held_type.removeprefix("Nullable(").startswith(CLICKHOUSE_UNSIGNED_TYPE_PREFIX):
            return lambda values: values
        floor = CLICKHOUSE_UNSIGNED_LITERAL_FLOOR

        def write_values(values: list[Any]) -> list[Any]:
            if not any(type(value) is int and value >= floor for value in values):
                return values
            return [
                ClickhouseTypedValue(value, held_type) if type(value) is int and value >= floor else value
                for value in values
            ]

        return write_values

    def prepare_python(self, field: Field[Any]) -> Callable[[Any], Any]:
        """Prepare the container's sole read traversal."""
        return field.get_dialect_python_reader(self.types)  # type: ignore[attr-defined]
