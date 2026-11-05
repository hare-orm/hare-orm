from __future__ import annotations

import copy
from collections.abc import Callable
from operator import attrgetter
from typing import Any, ClassVar, cast

from clickhouse_connect.datatypes import dynamic, registry
from clickhouse_connect.datatypes.container import Array, Map, Nested, Tuple
from clickhouse_connect.datatypes.special import SimpleAggregateFunction
from clickhouse_connect.driver.binding import quote_identifier
from clickhouse_connect.driver.exceptions import DataError

from hare.core.caching.cache import Cache
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_dynamic_type import (
    ClickhouseConnectDynamicType,
)
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_json_type import (
    ClickhouseConnectJsonType,
)
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_shared_variant_type import (
    ClickhouseConnectSharedVariantType,
)


class ClickhouseConnectColumnTypes:
    """Client-owned codec trees; cached library declarations are never mutated.

    Container implementations remain the library's. Their local copies refer to local child
    codecs, including Dynamic inside arrays, maps, tuples, variants and typed JSON paths.
    """

    declarations: ClassVar[Cache[Any]] = Cache(512, holds_sql=False)

    def __init__(self) -> None:
        self.columns = self.declarations.new_shared_bucket()
        self.shared_variant = ClickhouseConnectSharedVariantType(dynamic.SHARED_VARIANT_TYPE.type_def)

    def get_type(self, name: str) -> Any:
        """Get a codec with this factory's lifetime and recursively local child codecs."""
        if (column := self.columns.get(name)) is not None:
            return column
        original = registry.get_from_name(name)
        if isinstance(original, dynamic.Dynamic):
            column = ClickhouseConnectDynamicType(original.type_def, self)
        elif isinstance(original, dynamic.JSON):
            column = ClickhouseConnectJsonType(original.type_def, self)
        else:
            column = copy.copy(original)
            if isinstance(column, Array):
                column.element_type = self.get_type(column.element_type.name)
                column._insert_name = f"Array({column.element_type.insert_name})"
            elif isinstance(column, Map):
                column.key_type = self.get_type(column.key_type.name)
                column.value_type = self.get_type(column.value_type.name)
                column._insert_name = f"Map({column.key_type.insert_name}, {column.value_type.insert_name})"
            elif isinstance(column, Nested):
                tuple_array = self.get_type(column.tuple_array.name)
                column.tuple_array = tuple_array
                column.element_types = tuple_array.element_type.element_types
            elif isinstance(column, (Tuple, dynamic.Variant)):
                column.element_types = [self.get_type(element.name) for element in column.element_types]
                if isinstance(column, Tuple):
                    names = [element.insert_name for element in column.element_types]
                    if column.element_names:
                        names = [
                            f"{quote_identifier(key)} {value}"
                            for key, value in zip(column.element_names, names, strict=True)
                        ]
                    column._insert_name = f"Tuple({', '.join(names)})"
                elif isinstance(column, dynamic.Variant):
                    cast("Callable[[], None]", column._build_dispatch)()
            elif isinstance(column, SimpleAggregateFunction):
                column.element_type = self.get_type(column.element_type.name)
        self.columns[name] = column
        return column

    def read_dynamic_prefix(self, source: Any, context: Any) -> Any:
        """Read Dynamic's declared variants and attach the local shared-value decoder."""
        version = source.read_uint64()
        if version == 1:
            source.read_leb128()
        elif version != 2:
            raise DataError(f"Unsupported Dynamic structure version: {version}")
        variants = [self.get_type(source.read_leb128_str()) for _ in range(source.read_leb128())]
        variants.append(self.shared_variant)
        variants.sort(key=attrgetter("name"))
        if source.read_uint64() != 0:
            raise DataError("Unsupported Dynamic discriminator encoding")
        states = [column.read_column_prefix(source, context) for column in variants]
        return dynamic.DynamicState(version, variants, states)
