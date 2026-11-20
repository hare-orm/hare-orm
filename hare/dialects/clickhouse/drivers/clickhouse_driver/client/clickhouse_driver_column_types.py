from __future__ import annotations

import functools
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from clickhouse_driver.columns import service
from clickhouse_driver.columns.datetimecolumn import DateTime64Column, DateTimeColumn
from clickhouse_driver.columns.decimalcolumn import DecimalColumn
from clickhouse_driver.columns.newjsoncolumn import NewJsonColumn
from clickhouse_driver.columns.tuplecolumn import TupleColumn
from clickhouse_driver.columns.util import get_inner_columns, get_inner_spec

from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_dynamic_column import (
    ClickhouseDriverDynamicColumn,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_map_columns import (
    ClickhouseDriverMapColumns,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_moment_columns import (
    ClickhouseDriverMomentColumns,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_tuple_columns import (
    ClickhouseDriverTupleColumns,
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
from hare.dialects.clickhouse.enums import ClickhouseTypeName
from hare.dialects.clickhouse.types.constants import CLICKHOUSE_ESCAPED_JSON_KEY_DOT, CLICKHOUSE_JSON_KEY_DOT


class ClickhouseDriverColumnTypes:
    """The types clickhouse-driver reads and writes no column of, taught to it: ``Dynamic`` (the
    library reads one inside a ``JSON`` column only), ``Variant(T, ...)``, and the geometries
    ``LineString`` and ``MultiLineString`` - arrays of points, as the library reads a ``Ring`` and a
    ``Polygon``. Every column is built from its own copy of the library's options - the library builds
    no decimal after a ``DateTime64`` of the same container otherwise. A decimal is read at its column's
    scale - the library's division by the scale drops the zeros a value ends with. A ``JSON`` column's keys
    are read with their dots - the session escapes them (json_type_escape_dots_in_keys)."""

    @classmethod
    def get_column_by_specification(
        cls, specification: str, column_options: dict[str, Any], use_numpy: bool | None = None
    ) -> Any:
        """Build a column tree without modifying the library's registry or classes.

        Container constructors receive this factory explicitly. Each child receives a fresh
        options mapping, so a temporal column's scale cannot leak into its siblings. Native
        protocol framing and scalar codecs remain the library's responsibility.
        """
        options = dict(column_options)

        def child(specification: str) -> Any:
            return cls.get_column_by_specification(specification, options, use_numpy=use_numpy)

        for alias, primitive in CLICKHOUSE_DRIVER_GEOMETRY_ALIASES:
            if specification == alias:
                return child(primitive)
        is_dynamic = specification == ClickhouseTypeName.DYNAMIC or specification.startswith(
            CLICKHOUSE_DRIVER_DYNAMIC_PREFIX
        )
        if is_dynamic:
            return ClickhouseDriverDynamicColumn(child, **options)
        if specification.startswith(CLICKHOUSE_DRIVER_VARIANT_PREFIX):
            types = [
                part.strip() for part in get_inner_columns(get_inner_spec(ClickhouseTypeName.VARIANT, specification))
            ]
            return ClickhouseDriverVariantColumn(types, child, **options)
        if specification.startswith("Map("):
            return ClickhouseDriverMapColumns.create_map_column(specification, child, options)
        if specification.startswith("Nullable("):
            return service.create_nullable_column(specification, child)
        if specification.startswith("SimpleAggregateFunction("):
            return service.create_simple_aggregate_function_column(specification, child)
        if specification.startswith("Array("):
            column = service.create_array_column(specification, child, options)
        elif specification.startswith("Tuple("):
            column = service.create_tuple_column(specification, child, options)
        elif specification.startswith("Nested("):
            column = service.create_nested_column(specification, child, options)
        elif specification.startswith("LowCardinality("):
            column = service.create_low_cardinality_column(specification, child, options)
        elif specification.startswith("Object('json')"):
            column = service.create_json_column(specification, child, options)
        elif specification.startswith(ClickhouseTypeName.JSON):
            column = service.create_newjson_column(specification, child, options)
        else:
            # Geometry aliases supplied by the library can also contain hare's codecs.
            for alias, primitive in service.aliases:
                if specification == alias:
                    return child(primitive)
            column = service.get_column_by_spec(specification, options, use_numpy=use_numpy)
        if isinstance(column, DecimalColumn):
            column.after_read_items = functools.partial(cls.read_decimals, column.scale)
        elif isinstance(column, NewJsonColumn):
            column.after_read_items = cls.read_json_objects
        elif isinstance(column, DateTime64Column):
            column.after_read_items = functools.partial(ClickhouseDriverMomentColumns.read_moments, column)
        elif isinstance(column, DateTimeColumn):
            column.after_read_items = functools.partial(ClickhouseDriverMomentColumns.read_whole_seconds, column)
        elif isinstance(column, TupleColumn):
            column.write_data = functools.partial(ClickhouseDriverTupleColumns.write_data, column)
        return column

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
