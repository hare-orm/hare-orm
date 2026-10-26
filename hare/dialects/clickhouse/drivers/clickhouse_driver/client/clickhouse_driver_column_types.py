from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any, ClassVar

from clickhouse_driver.columns import service
from clickhouse_driver.columns.decimalcolumn import DecimalColumn
from clickhouse_driver.columns.newjsoncolumn import NewJsonColumn
from clickhouse_driver.columns.util import get_inner_columns, get_inner_spec

from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_dynamic_column import (
    ClickhouseDriverDynamicColumn,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_variant_column import (
    ClickhouseDriverVariantColumn,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import (
    CLICKHOUSE_DRIVER_DECIMAL_CONTEXT,
    CLICKHOUSE_DRIVER_DYNAMIC_PREFIX,
    CLICKHOUSE_DRIVER_GEOMETRY_ALIASES,
    CLICKHOUSE_DRIVER_VARIANT_PREFIX,
)
from hare.dialects.clickhouse.types.constants import (
    CLICKHOUSE_DYNAMIC_TYPE,
    CLICKHOUSE_ESCAPED_JSON_KEY_DOT,
    CLICKHOUSE_JSON_KEY_DOT,
)


class ClickhouseDriverColumnTypes:
    """The types clickhouse-driver reads and writes no column of, taught to it: ``Dynamic`` (the
    library reads one inside a ``JSON`` column only), ``Variant(T, ...)``, and the geometries
    ``LineString`` and ``MultiLineString`` - arrays of points, as the library reads a ``Ring`` and a
    ``Polygon``. Every column is built from its own copy of the library's options - the library builds
    no decimal after a ``DateTime64`` of the same container otherwise. A decimal is read at its column's
    scale - the library's division by the scale drops the zeros a value ends with. A ``JSON`` column's keys
    are read with their dots - the session escapes them (json_type_escape_dots_in_keys)."""

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
            column = cls.library_getters[0](specification, column_options, use_numpy=use_numpy)
            if isinstance(column, DecimalColumn):
                column.after_read_items = functools.partial(cls.read_decimals, column.scale)
            elif isinstance(column, NewJsonColumn):
                column.after_read_items = cls.read_json_objects
            return column

        def get_held_column(held_specification: str) -> Any:
            return service.get_column_by_spec(held_specification, column_options, use_numpy=use_numpy)

        if is_dynamic:
            return ClickhouseDriverDynamicColumn(get_held_column, **column_options)
        variant_types = [part.strip() for part in get_inner_columns(get_inner_spec("Variant", specification))]
        return ClickhouseDriverVariantColumn(variant_types, get_held_column, **column_options)

    @classmethod
    def read_json_objects(cls, items: Sequence[Any], nulls_map: Sequence[bool] | None = None) -> tuple[Any, ...]:
        """The objects of a ``JSON`` column, a dot of each key read back from its escape.

        Args:
            items: The objects as the library reads them.
            nulls_map: Which values are NULL, None for a column holding no NULL.

        Returns:
            The objects.
        """
        objects = tuple(cls.get_key_dots_read(item) for item in items)
        if nulls_map is None:
            return objects
        return tuple(None if is_null else item for item, is_null in zip(objects, nulls_map, strict=True))

    @classmethod
    def get_key_dots_read(cls, value: Any) -> Any:
        """A value read from a ``JSON`` column with the dots of its objects' keys unescaped - its arrays and
        nested objects too.

        Args:
            value: The value.

        Returns:
            The value - itself when it holds no object.
        """
        if isinstance(value, dict):
            return {
                key.replace(CLICKHOUSE_ESCAPED_JSON_KEY_DOT, CLICKHOUSE_JSON_KEY_DOT): cls.get_key_dots_read(member)
                for key, member in value.items()
            }
        if isinstance(value, list):
            return [cls.get_key_dots_read(member) for member in value]
        return value

    @staticmethod
    def read_decimals(
        scale: int, items: Sequence[int], nulls_map: Sequence[bool] | None = None
    ) -> tuple[Decimal | None, ...]:
        """The values of a decimal column, each at the column's scale.

        Args:
            scale: The column's scale.
            items: The values as the whole numbers stored.
            nulls_map: Which values are NULL, None for a column holding no NULL.

        Returns:
            The values.
        """
        exponent = -scale
        context = CLICKHOUSE_DRIVER_DECIMAL_CONTEXT
        scaleb = Decimal.scaleb
        if nulls_map is None:
            return tuple(scaleb(Decimal(item), exponent, context) for item in items)
        return tuple(
            None if is_null else scaleb(Decimal(item), exponent, context)
            for item, is_null in zip(items, nulls_map, strict=True)
        )
