from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from enum import Enum, IntEnum, StrEnum
from typing import Any

from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_DATETIME_COLUMN_TYPE,
    CLICKHOUSE_DATETIME_FIELD_PATH,
    CLICKHOUSE_DEFAULT_DATETIME_FIELD_PATH,
    CLICKHOUSE_TYPE_MAP,
)
from hare.dialects.clickhouse.enums import ClickhouseTypeName
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_ARRAY_FIELD_PATH,
    CLICKHOUSE_CHAR_ENUM_FIELD_PATH,
    CLICKHOUSE_DYNAMIC_FIELD_PATH,
    CLICKHOUSE_DYNAMIC_MAX_TYPES_ARGUMENT,
    CLICKHOUSE_ENUM_LABEL_FIELD_PATH,
    CLICKHOUSE_FALLBACK_FIELD_PATH,
    CLICKHOUSE_INSPECTED_ENUM_NAME,
    CLICKHOUSE_INT_ENUM_FIELD_PATH,
    CLICKHOUSE_LOW_CARDINALITY_FIELD_PATH,
    CLICKHOUSE_MAP_FIELD_PATH,
    CLICKHOUSE_NESTED_FIELD_PATH,
    CLICKHOUSE_TUPLE_FIELD_PATH,
    CLICKHOUSE_VARIANT_FIELD_PATH,
)
from hare.dialects.clickhouse.types.clickhouse_enum_types import ClickhouseEnumTypes
from hare.dialects.clickhouse.types.clickhouse_physical_type import ClickhousePhysicalType
from hare.inspectdb.constants import INSPECTED_ENUM_MODULE_NAME
from hare.inspectdb.generation.model_naming import ModelNaming
from hare.inspectdb.introspection.inspected_field_specification import InspectedFieldSpecification


class ClickhouseTypeParser:
    """Reads a ClickHouse column type, as ``system.columns`` reports it, into the field it maps to -
    containers to any depth: ``Array(Map(String, Array(Nullable(Int32))))`` is an ``ArrayField`` of a
    ``MapField`` of a ``TextField`` and an ``ArrayField`` of a nullable ``IntField``."""

    #: The spec of a type no field maps to - its values read as text.
    FALLBACK_SPECIFICATION = InspectedFieldSpecification(CLICKHOUSE_FALLBACK_FIELD_PATH, is_ambiguous=True)

    @staticmethod
    def unwrap(type_sql: str) -> tuple[str, bool]:
        """A type without its ``Nullable(...)``, and whether it holds NULLs.

        Args:
            type_sql: The type.

        Returns:
            The type, and whether it is nullable.
        """
        physical_type = ClickhousePhysicalType.from_sql(type_sql)
        if physical_type.name == ClickhouseTypeName.NULLABLE and len(physical_type.elements) == 1:
            return physical_type.elements[0].sql, True
        return type_sql, False

    @classmethod
    def get_named_elements(
        cls, physical_type: ClickhousePhysicalType, enum_name: str = CLICKHOUSE_INSPECTED_ENUM_NAME
    ) -> dict[str, InspectedFieldSpecification] | None:
        """Field specifications of a tuple whose elements all have catalog names."""
        if not physical_type.elements or not all(name is not None for name in physical_type.element_names):
            return None
        return {
            name: cls.get_physical_specification(element, enum_name)  # type: ignore[misc]
            for name, element in zip(physical_type.element_names, physical_type.elements, strict=True)
        }

    @classmethod
    def get_specification(
        cls, type_sql: str, enum_name: str = CLICKHOUSE_INSPECTED_ENUM_NAME
    ) -> InspectedFieldSpecification:
        """The field a type maps to.

        Args:
            type_sql: The type.
            enum_name: The name an enum class read off the type is named after.

        Returns:
            The field's specification - a ``TextField`` for a type no field maps.
        """
        return cls.get_physical_specification(ClickhousePhysicalType.from_sql(type_sql), enum_name)

    @classmethod
    def get_physical_specification(
        cls, physical_type: ClickhousePhysicalType, enum_name: str
    ) -> InspectedFieldSpecification:
        """Map an existing type tree without reparsing each container's declaration."""
        if physical_type.name == ClickhouseTypeName.NULLABLE and len(physical_type.elements) == 1:
            specification = cls.get_physical_specification(physical_type.elements[0], enum_name)
            return replace(specification, kwargs={**specification.kwargs, "null": True})
        return cls.get_unwrapped_specification(physical_type, enum_name)

    @classmethod
    def get_array_specification(
        cls, element_type: ClickhousePhysicalType, enum_name: str
    ) -> InspectedFieldSpecification:
        """Named tuple arrays map to NestedField; other arrays retain their element field."""
        if element_type.name == ClickhouseTypeName.TUPLE:
            named = cls.get_named_elements(element_type, enum_name)
            if named is not None:
                return InspectedFieldSpecification(CLICKHOUSE_NESTED_FIELD_PATH, {"element_fields": named})
        return InspectedFieldSpecification(
            CLICKHOUSE_ARRAY_FIELD_PATH, {"base_field": cls.get_physical_specification(element_type, enum_name)}
        )

    @classmethod
    def get_unwrapped_specification(
        cls, physical_type: ClickhousePhysicalType, enum_name: str = CLICKHOUSE_INSPECTED_ENUM_NAME
    ) -> InspectedFieldSpecification:
        """The field a type without wrappers maps to.

        Args:
            physical_type: The parsed type without its nullable wrapper.
            enum_name: The name an enum class read off the type is named after.

        Returns:
            The field's specification.
        """
        type_name, arguments = physical_type.name, physical_type.arguments
        elements = physical_type.elements
        if type_name == ClickhouseTypeName.ARRAY and len(arguments) == 1:
            return cls.get_array_specification(elements[0], enum_name)
        if type_name == ClickhouseTypeName.NESTED and arguments:
            named = cls.get_named_elements(physical_type, enum_name)
            if named is not None:
                return InspectedFieldSpecification(CLICKHOUSE_NESTED_FIELD_PATH, {"element_fields": named})
        if type_name == ClickhouseTypeName.MAP and len(arguments) == 2:
            return InspectedFieldSpecification(
                CLICKHOUSE_MAP_FIELD_PATH,
                {
                    "key_field": cls.get_physical_specification(elements[0], enum_name),
                    "value_field": cls.get_physical_specification(elements[1], enum_name),
                },
            )
        if type_name == ClickhouseTypeName.LOW_CARDINALITY and len(arguments) == 1:
            base_specification = cls.get_physical_specification(elements[0], enum_name)
            base_kwargs = {name: value for name, value in base_specification.kwargs.items() if name != "null"}
            return InspectedFieldSpecification(
                CLICKHOUSE_LOW_CARDINALITY_FIELD_PATH,
                {
                    "base_field": replace(base_specification, kwargs=base_kwargs),
                    **({"null": True} if base_specification.kwargs.get("null") else {}),
                },
            )
        if type_name in {ClickhouseTypeName.ENUM8, ClickhouseTypeName.ENUM16} and arguments:
            return cls.get_enum_specification(type_name, arguments, enum_name)
        if type_name == ClickhouseTypeName.DYNAMIC:
            max_types = [
                int(argument.removeprefix(CLICKHOUSE_DYNAMIC_MAX_TYPES_ARGUMENT).strip())
                for argument in arguments
                if argument.startswith(CLICKHOUSE_DYNAMIC_MAX_TYPES_ARGUMENT)
            ]
            return InspectedFieldSpecification(
                CLICKHOUSE_DYNAMIC_FIELD_PATH, {"max_types": max_types[0]} if max_types else {}
            )
        if type_name == ClickhouseTypeName.VARIANT and arguments:
            return InspectedFieldSpecification(
                CLICKHOUSE_VARIANT_FIELD_PATH,
                {"variant_fields": [cls.get_physical_specification(element, enum_name) for element in elements]},
            )
        if type_name == ClickhouseTypeName.TUPLE and arguments:
            named = cls.get_named_elements(physical_type, enum_name)
            element_fields: Any = (
                named
                if named is not None
                else [cls.get_physical_specification(element, enum_name) for element in elements]
            )
            return InspectedFieldSpecification(CLICKHOUSE_TUPLE_FIELD_PATH, {"element_fields": element_fields})
        lowered_name = type_name.strip().lower()
        for mapped_type_name, path, default_kwargs in CLICKHOUSE_TYPE_MAP:
            if mapped_type_name != lowered_name:
                continue
            kwargs = dict(default_kwargs)
            if lowered_name == "decimal" and len(arguments) == 2:
                kwargs["max_digits"], kwargs["decimal_places"] = physical_type.precision, physical_type.scale
            if lowered_name == "fixedstring" and len(arguments) == 1:
                kwargs["length"] = int(arguments[0])
            if lowered_name in {"datetime", "datetime64"}:
                return ClickhouseTypeParser.get_moment_specification(physical_type)
            return InspectedFieldSpecification(path, kwargs)
        return ClickhouseTypeParser.FALLBACK_SPECIFICATION

    @staticmethod
    def get_moment_specification(physical_type: ClickhousePhysicalType) -> InspectedFieldSpecification:
        """Map catalog precision/timezone without inventing a timezone for an unzoned type."""
        if physical_type.sql == CLICKHOUSE_DATETIME_COLUMN_TYPE:
            return InspectedFieldSpecification(CLICKHOUSE_DEFAULT_DATETIME_FIELD_PATH, {})
        kwargs: dict[str, Any] = {}
        if physical_type.name == ClickhouseTypeName.DATETIME64 and physical_type.precision is not None:
            kwargs["precision"] = physical_type.precision
        if physical_type.timezone != "UTC":
            kwargs["timezone"] = physical_type.timezone
        return InspectedFieldSpecification(CLICKHOUSE_DATETIME_FIELD_PATH, kwargs)

    @staticmethod
    def get_enum_specification(
        type_name: str, arguments: Sequence[str], enum_name: str
    ) -> InspectedFieldSpecification:
        """The field an enum maps to - a ``CharEnumField`` when its labels are numbered as one numbers
        its values, an ``IntEnumField`` when its labels are member names, each only when it writes the
        same ``Enum8``/``Enum16``; a ``CharField`` of the labels otherwise.

        Args:
            type_name: ``Enum8`` or ``Enum16``.
            arguments: The members as the type names them.
            enum_name: The name the enum class is named after.

        Returns:
            The field's specification.
        """
        numbers_by_label = ClickhousePhysicalType.get_enum_members(arguments)
        labels = list(numbers_by_label)
        class_name = ModelNaming.get_class_name(enum_name)
        enum_field: tuple[str, type[Enum]] | None = None
        if numbers_by_label == ClickhouseEnumTypes.get_label_numbers(labels):
            member_names = ModelNaming.get_enum_member_names(labels)
            enum_field = (  # type: ignore[assignment]
                CLICKHOUSE_CHAR_ENUM_FIELD_PATH,
                StrEnum(class_name, member_names, module=INSPECTED_ENUM_MODULE_NAME),
            )
        elif all(label.isidentifier() and not label.startswith("_") for label in labels):
            enum_field = (  # type: ignore[assignment]
                CLICKHOUSE_INT_ENUM_FIELD_PATH,
                IntEnum(class_name, numbers_by_label, module=INSPECTED_ENUM_MODULE_NAME),
            )
        written_type_name = ClickhouseEnumTypes.get_enum_type(numbers_by_label).partition("(")[0]
        if enum_field is None or written_type_name != type_name:
            return InspectedFieldSpecification(
                CLICKHOUSE_ENUM_LABEL_FIELD_PATH, {"max_length": max(len(label) for label in labels)}
            )
        field_path, enum_type = enum_field
        return InspectedFieldSpecification(field_path, {"enum_type": enum_type})

    @classmethod
    def holds_only_mapped_types(cls, value: Any) -> bool:
        """Whether no spec held in a value is the fallback one.

        Args:
            value: A spec, or arguments holding specs.

        Returns:
            True when none is.
        """
        if isinstance(value, InspectedFieldSpecification):
            return not value.is_ambiguous and cls.holds_only_mapped_types(value.kwargs)
        if isinstance(value, dict):
            return all(cls.holds_only_mapped_types(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return all(cls.holds_only_mapped_types(item) for item in value)
        return True
