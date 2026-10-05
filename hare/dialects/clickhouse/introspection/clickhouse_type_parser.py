from __future__ import annotations

from typing import Any

from hare.dialects.clickhouse.constants import CLICKHOUSE_TYPE_MAP
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_ARRAY_FIELD_PATH,
    CLICKHOUSE_DYNAMIC_FIELD_PATH,
    CLICKHOUSE_DYNAMIC_MAX_TYPES_ARGUMENT,
    CLICKHOUSE_ENUM_LABEL_FIELD_PATH,
    CLICKHOUSE_FALLBACK_FIELD_PATH,
    CLICKHOUSE_LOW_CARDINALITY_FIELD_PATH,
    CLICKHOUSE_MAP_FIELD_PATH,
    CLICKHOUSE_NESTED_FIELD_PATH,
    CLICKHOUSE_TUPLE_FIELD_PATH,
    CLICKHOUSE_VARIANT_FIELD_PATH,
)
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.dialects.clickhouse.types.constants import CLICKHOUSE_NULLABLE_TYPE
from hare.inspectdb.introspection.inspected_field_specification import InspectedFieldSpecification


class ClickhouseTypeParser:
    """Reads a ClickHouse column type, as ``system.columns`` reports it, into the field it maps to -
    containers to any depth: ``Array(Map(String, Array(Nullable(Int32))))`` is an ``ArrayField`` of a
    ``MapField`` of a ``TextField`` and an ``ArrayField`` of a nullable ``IntField``."""

    #: The spec of a type no field maps to - its values read as text.
    FALLBACK_SPECIFICATION = InspectedFieldSpecification(CLICKHOUSE_FALLBACK_FIELD_PATH, {})

    @staticmethod
    def unwrap(type_sql: str) -> tuple[str, bool]:
        """A type without its ``Nullable(...)``, and whether it holds NULLs.

        Args:
            type_sql: The type.

        Returns:
            The type, and whether it is nullable.
        """
        name, arguments = ClickhouseTypeNames.get_type_parts(type_sql)
        if name == CLICKHOUSE_NULLABLE_TYPE and len(arguments) == 1:
            return arguments[0], True
        return type_sql, False

    @classmethod
    def get_named_elements(cls, arguments: list[str]) -> dict[str, InspectedFieldSpecification] | None:
        """The elements of a tuple by name, when every element is named (``name Type``).

        Args:
            arguments: The tuple's arguments.

        Returns:
            The element fields by name, None for a tuple of unnamed elements.
        """
        named: dict[str, InspectedFieldSpecification] = {}
        for argument in arguments:
            name, _separator, element_type = argument.partition(" ")
            if not element_type or not name.isidentifier() or "(" in name:
                return None
            named[name] = cls.get_specification(element_type.strip())
        return named

    @classmethod
    def get_specification(cls, type_sql: str) -> InspectedFieldSpecification:
        """The field a type maps to.

        Args:
            type_sql: The type.

        Returns:
            The field's specification - a ``TextField`` for a type no field maps.
        """
        unwrapped_sql, nullable = cls.unwrap(type_sql.strip())
        specification = cls.get_unwrapped_specification(unwrapped_sql)
        if nullable:
            return InspectedFieldSpecification(specification.path, {**specification.kwargs, "null": True})
        return specification

    @classmethod
    def get_array_specification(cls, element_type_sql: str) -> InspectedFieldSpecification:
        """The field an array maps to - a nested field for an array of tuples whose elements are all
        named, an array field for any other.

        Args:
            element_type_sql: The type of the array's elements.

        Returns:
            The field's specification.
        """
        element_type, _nullable = cls.unwrap(element_type_sql)
        if element_type.startswith("Tuple("):
            named = cls.get_named_elements(ClickhouseSqlParts.split(element_type[len("Tuple(") : -1]))
            if named is not None:
                return InspectedFieldSpecification(CLICKHOUSE_NESTED_FIELD_PATH, {"element_fields": named})
        return InspectedFieldSpecification(
            CLICKHOUSE_ARRAY_FIELD_PATH, {"base_field": cls.get_specification(element_type_sql)}
        )

    @classmethod
    def get_unwrapped_specification(cls, type_sql: str) -> InspectedFieldSpecification:
        """The field a type without wrappers maps to.

        Args:
            type_sql: The type.

        Returns:
            The field's specification.
        """
        type_name, _separator, rest = type_sql.partition("(")
        arguments = ClickhouseSqlParts.split(rest[:-1]) if rest.endswith(")") else []
        if type_name == "Array" and len(arguments) == 1:
            return cls.get_array_specification(arguments[0])
        if type_name == "Nested" and arguments:
            named = cls.get_named_elements(arguments)
            if named is not None:
                return InspectedFieldSpecification(CLICKHOUSE_NESTED_FIELD_PATH, {"element_fields": named})
        if type_name == "Map" and len(arguments) == 2:
            return InspectedFieldSpecification(
                CLICKHOUSE_MAP_FIELD_PATH,
                {"key_field": cls.get_specification(arguments[0]), "value_field": cls.get_specification(arguments[1])},
            )
        if type_name == "LowCardinality" and len(arguments) == 1:
            base_specification = cls.get_specification(arguments[0])
            base_kwargs = {name: value for name, value in base_specification.kwargs.items() if name != "null"}
            return InspectedFieldSpecification(
                CLICKHOUSE_LOW_CARDINALITY_FIELD_PATH,
                {
                    "base_field": InspectedFieldSpecification(base_specification.path, base_kwargs),
                    **({"null": True} if base_specification.kwargs.get("null") else {}),
                },
            )
        if type_name in {"Enum8", "Enum16"} and arguments:
            labels = [argument.rpartition("=")[0].strip()[1:-1] for argument in arguments]
            return InspectedFieldSpecification(
                CLICKHOUSE_ENUM_LABEL_FIELD_PATH, {"max_length": max(len(label) for label in labels)}
            )
        if type_name == "Dynamic":
            max_types = [
                int(argument.removeprefix(CLICKHOUSE_DYNAMIC_MAX_TYPES_ARGUMENT).strip())
                for argument in arguments
                if argument.startswith(CLICKHOUSE_DYNAMIC_MAX_TYPES_ARGUMENT)
            ]
            return InspectedFieldSpecification(
                CLICKHOUSE_DYNAMIC_FIELD_PATH, {"max_types": max_types[0]} if max_types else {}
            )
        if type_name == "Variant" and arguments:
            return InspectedFieldSpecification(
                CLICKHOUSE_VARIANT_FIELD_PATH,
                {"variant_fields": [cls.get_specification(argument) for argument in arguments]},
            )
        if type_name == "Tuple" and arguments:
            named = cls.get_named_elements(arguments)
            element_fields: Any = (
                named if named is not None else [cls.get_specification(argument) for argument in arguments]
            )
            return InspectedFieldSpecification(CLICKHOUSE_TUPLE_FIELD_PATH, {"element_fields": element_fields})
        lowered_name = type_name.strip().lower()
        for mapped_type_name, path, default_kwargs in CLICKHOUSE_TYPE_MAP:
            if mapped_type_name != lowered_name:
                continue
            kwargs = dict(default_kwargs)
            if lowered_name == "decimal" and len(arguments) == 2:
                kwargs["max_digits"], kwargs["decimal_places"] = int(arguments[0]), int(arguments[1])
            if lowered_name == "fixedstring" and len(arguments) == 1:
                kwargs["length"] = int(arguments[0])
            return InspectedFieldSpecification(path, kwargs)
        return ClickhouseTypeParser.FALLBACK_SPECIFICATION

    @classmethod
    def holds_only_mapped_types(cls, value: Any) -> bool:
        """Whether no spec held in a value is the fallback one.

        Args:
            value: A spec, or arguments holding specs.

        Returns:
            True when none is.
        """
        if isinstance(value, InspectedFieldSpecification):
            return value is not cls.FALLBACK_SPECIFICATION and cls.holds_only_mapped_types(value.kwargs)
        if isinstance(value, dict):
            return all(cls.holds_only_mapped_types(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return all(cls.holds_only_mapped_types(item) for item in value)
        return True
