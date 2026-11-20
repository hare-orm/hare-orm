from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any

from clickhouse_connect.driver.query import QueryResult
from clickhouse_connect.driver.transform import NativeTransform

from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_column_types import (
    ClickhouseConnectColumnTypes,
)
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_response import (
    ClickhouseConnectResponse,
)


class ClickhouseConnectTransform:
    """Client-local Native codecs with the library's standard HTTP and insert transport.

    Assigned to one AsyncClient's Transform boundary. No registry, datatype class, decoder
    function or other client is changed. Insert framing and compression use NativeTransform.
    """

    threaded_insert = False

    def __init__(self) -> None:
        self.types = ClickhouseConnectColumnTypes()

    def parse_response(self, source: Any, context: Any) -> QueryResult:
        """Create a lazy result whose column trees belong to this client."""
        response = ClickhouseConnectResponse(source, context, self.types)
        first = response.read_block()
        if first is None:
            source.close()
            return QueryResult([])
        return QueryResult(
            None,
            response.blocks(first),
            tuple(response.names),
            tuple(response.column_types),
            context.column_oriented,
            source,
        )

    def build_insert(self, context: Any, error_handler: Callable[[Exception], None] | None = None) -> Generator[bytes]:
        """Encode with local column types through the library's normal block writer."""
        self.prepare_insert(context)
        return NativeTransform.build_insert(context, error_handler)

    def prepare_insert(self, context: Any) -> None:
        """Select local codecs before InsertContext estimates the size of its input rows."""
        context.column_types = [self.types.get_type(column.name) for column in context.column_types]
