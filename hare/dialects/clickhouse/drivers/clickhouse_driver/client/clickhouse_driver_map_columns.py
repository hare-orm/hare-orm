from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from clickhouse_driver.columns import service
from clickhouse_driver.columns.mapcolumn import MapColumn
from clickhouse_driver.columns.util import get_inner_columns, get_inner_spec


class ClickhouseDriverMapColumns:
    """The reading of a ``Map(K, V)`` type by clickhouse-driver, made exact at any depth. The library
    splits ``K, V`` on the commas outside one level of parentheses - a value type holding a container
    of several types (``Map(String, Array(Tuple(Int32, String)))``) splits into more than two. The
    two types are split as the library splits a tuple's, by the depth of the parentheses."""

    #: The library's own builder of a map column - kept to tell it was replaced.
    library_builders: ClassVar[list[Callable[..., Any]]] = []

    @classmethod
    def install(cls) -> None:
        """Replaces the library's builder of map columns - once."""
        if cls.library_builders:
            return
        cls.library_builders.append(service.create_map_column)
        service.create_map_column = cls.create_map_column

    @staticmethod
    def create_map_column(
        specification: str, column_by_specification_getter: Callable[[str], Any], column_options: dict[str, Any]
    ) -> Any:
        """The library's column of a ``Map(K, V)`` type.

        Args:
            specification: The type.
            column_by_specification_getter: Builds the column of a type.
            column_options: The library's column options.

        Returns:
            The column.
        """
        key_specification, value_specification = (
            part.strip() for part in get_inner_columns(get_inner_spec("Map", specification))
        )
        return MapColumn(
            column_by_specification_getter(key_specification),
            column_by_specification_getter(value_specification),
            **column_options,
        )
