from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from clickhouse_driver.columns import service
from clickhouse_driver.columns.util import get_inner_columns, get_inner_spec

from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_dynamic_column import (
    ClickhouseDriverDynamicColumn,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_variant_column import (
    ClickhouseDriverVariantColumn,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import (
    CLICKHOUSE_DRIVER_DYNAMIC_PREFIX,
    CLICKHOUSE_DRIVER_GEOMETRY_ALIASES,
    CLICKHOUSE_DRIVER_VARIANT_PREFIX,
)
from hare.dialects.clickhouse.types.constants import CLICKHOUSE_DYNAMIC_TYPE


class ClickhouseDriverColumnTypes:
    """The types clickhouse-driver reads and writes no column of, taught to it: ``Dynamic`` (the
    library reads one inside a ``JSON`` column only), ``Variant(T, ...)``, and the geometries
    ``LineString`` and ``MultiLineString`` - arrays of points, as the library reads a ``Ring`` and a
    ``Polygon``. Every column is built from its own copy of the library's options - the library builds
    no decimal after a ``DateTime64`` of the same container otherwise."""

    #: The library's own builder of a column of a type - kept to tell it was replaced.
    library_getters: ClassVar[list[Callable[..., Any]]] = []

    @classmethod
    def install(cls) -> None:
        """Teaches the library the types - once."""
        if cls.library_getters:
            return
        cls.library_getters.append(service.get_column_by_spec)
        service.get_column_by_spec = cls.get_column_by_specification
        service.aliases.extend(CLICKHOUSE_DRIVER_GEOMETRY_ALIASES)

    @classmethod
    def get_column_by_specification(
        cls, specification: str, column_options: dict[str, Any], use_numpy: bool | None = None
    ) -> Any:
        """The library's column of a type.

        Args:
            specification: The type.
            column_options: The library's column options.
            use_numpy: Whether the column reads into NumPy arrays - the library's choice when None.

        Returns:
            The column.
        """

        # A copy for each column: the library writes the scale of a DateTime64 into the options it is
        # given, which a decimal built from the same options then takes twice.
        column_options = dict(column_options)
        is_dynamic = specification == CLICKHOUSE_DYNAMIC_TYPE or specification.startswith(
            CLICKHOUSE_DRIVER_DYNAMIC_PREFIX
        )
        if not is_dynamic and not specification.startswith(CLICKHOUSE_DRIVER_VARIANT_PREFIX):
            return cls.library_getters[0](specification, column_options, use_numpy=use_numpy)

        def get_held_column(held_specification: str) -> Any:
            return service.get_column_by_spec(held_specification, column_options, use_numpy=use_numpy)

        if is_dynamic:
            return ClickhouseDriverDynamicColumn(get_held_column, **column_options)
        variant_types = [part.strip() for part in get_inner_columns(get_inner_spec("Variant", specification))]
        return ClickhouseDriverVariantColumn(variant_types, get_held_column, **column_options)
