from __future__ import annotations

import re

from hare.dialects.clickhouse.types.clickhouse_physical_type import ClickhousePhysicalType
from hare.dialects.clickhouse.types.constants import (
    CLICKHOUSE_ISO_TYPE_NAME_PATTERN,
    CLICKHOUSE_ISO_TYPE_NAMES,
    CLICKHOUSE_NULLABLE_TYPE,
    CLICKHOUSE_READ_PYTHON_TYPES,
)


class ClickhouseTypeNames:
    """ClickHouse's own names of types - the names the server reports a type by, the parts of a type's
    name, and the Python type the drivers read a type's values as."""

    @staticmethod
    def get_server_type(type_sql: str) -> str:
        """The type as the server names it - each ISO name hare's fields write (``INT``, ``VARCHAR(n)``)
        replaced by ClickHouse's own, at any depth; quoted text (an enum's labels) as it is.

        Args:
            type_sql: The type.

        Returns:
            The type.
        """

        def get_replacement(match: re.Match[str]) -> str:
            iso_name = match.group(1)
            return match.group(0) if iso_name is None else CLICKHOUSE_ISO_TYPE_NAMES[iso_name]

        return CLICKHOUSE_ISO_TYPE_NAME_PATTERN.sub(get_replacement, type_sql)

    @staticmethod
    def get_type_parts(type_text: str) -> tuple[str, list[str]]:
        """A type's name and the texts of its arguments.

        Args:
            type_text: The type, as the server names it.

        Returns:
            The name and the arguments - none for a type without them.
        """
        physical_type = ClickhousePhysicalType.from_sql(type_text)
        return physical_type.name, list(physical_type.arguments)

    @staticmethod
    def get_type_without_nullable(type_sql: str) -> str:
        """A column's type without the ``Nullable`` a nullable column has - around it, or inside its
        ``LowCardinality``.

        Args:
            type_sql: The type.

        Returns:
            The type.
        """
        physical_type = ClickhousePhysicalType.from_sql(type_sql)
        if physical_type.name == CLICKHOUSE_NULLABLE_TYPE and len(physical_type.elements) == 1:
            return physical_type.elements[0].sql
        if physical_type.name == "LowCardinality" and len(physical_type.elements) == 1:
            inner = physical_type.elements[0]
            if inner.name == CLICKHOUSE_NULLABLE_TYPE and len(inner.elements) == 1:
                return f"LowCardinality({inner.elements[0].sql})"
        return type_sql

    @classmethod
    def get_merged_type(cls, first_type: str, second_type: str) -> str | None:
        """The type holding the values of two types - one of them where the other holds no value
        (``Nothing``, an empty array's ``Array(Nothing)``, ``Nullable(Nothing)``), at any depth.

        Args:
            first_type: A type, as the server names it.
            second_type: The other one.

        Returns:
            The type; None when the two are other types.
        """
        if first_type == second_type:
            return first_type
        if first_type == "Nothing":
            return second_type
        if second_type == "Nothing":
            return first_type
        first_name, first_arguments = cls.get_type_parts(first_type)
        second_name, second_arguments = cls.get_type_parts(second_type)
        if first_name == "Nullable" or second_name == "Nullable":
            first_inner = first_arguments[0] if first_name == "Nullable" else first_type
            second_inner = second_arguments[0] if second_name == "Nullable" else second_type
            merged_inner = cls.get_merged_type(first_inner, second_inner)
            return None if merged_inner is None else f"Nullable({merged_inner})"
        if (
            first_name != second_name
            or len(first_arguments) != len(second_arguments)
            or first_name not in {"Array", "Map", "Tuple"}
        ):
            return None
        merged_arguments = [
            cls.get_merged_type(first_argument, second_argument)
            for first_argument, second_argument in zip(first_arguments, second_arguments, strict=True)
        ]
        if None in merged_arguments:
            return None
        return f"{first_name}({', '.join(merged_arguments)})"  # type: ignore[arg-type]

    @staticmethod
    def get_read_python_types(server_type: str) -> tuple[type, ...]:
        """The Python types the drivers read the values of a type as - a ``LowCardinality`` and a
        ``Nullable`` type's values as those of the type inside.

        Args:
            server_type: The type, as the server names it.

        Returns:
            The Python types - none for a type of no known one.
        """
        physical_type = ClickhousePhysicalType.from_sql(server_type).unwrapped
        for prefix, python_types in CLICKHOUSE_READ_PYTHON_TYPES:
            if physical_type.name.startswith(prefix):
                return python_types
        return ()
