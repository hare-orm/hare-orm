from __future__ import annotations

from typing import Any

from clickhouse_connect.datatypes import dynamic
from clickhouse_connect.driver.exceptions import DataError


class ClickhouseConnectJsonType(dynamic.JSON, registered=False):
    """JSON with local codecs for declared types and dynamically typed paths."""

    def __init__(self, type_def: Any, types: Any) -> None:
        super().__init__(type_def)
        self.types = types
        self.typed_types = [types.get_type(column.name) for column in self.typed_types]

    def read_column_prefix(self, source: Any, context: Any) -> Any:
        """Read JSON structure while binding each dynamic path to this client's factory."""
        version = source.read_uint64()
        if version == 0:
            source.read_leb128()
        elif version != 2:
            raise DataError(f"Unsupported JSON structure version: {version}")
        path_count = source.read_leb128()
        paths = [source.read_leb128_str() for _ in range(path_count)]
        typed_states = [column.read_column_prefix(source, context) for column in self.typed_types]
        dynamic_states = [self.types.read_dynamic_prefix(source, context) for _ in range(path_count)]
        shared_state = dynamic.SHARED_DATA_TYPE.read_column_prefix(source, context)
        return dynamic.JSONState(version, paths, typed_states, dynamic_states, shared_state)
