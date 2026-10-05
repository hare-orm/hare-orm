from __future__ import annotations

from typing import ClassVar

from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT, CLICKHOUSE_NATIVE_JSON_DIALECT
from hare.dialects.clickhouse.query.declarations import ClickhouseNativeJsonQuery, ClickhouseQuery
from hare.sql.builder.queries.query import Query


class ClickhouseDialectVariants:
    """The dialect a ClickHouse connection renders its SQL in, with its query class, by what its driver
    reads - a ``JSON`` column or the JSON text."""

    #: The dialect and the query class by whether a ``JSONField`` is a ``JSON`` column.
    VARIANTS: ClassVar[dict[bool, tuple[ClickhouseDialect, type[Query]]]] = {
        False: (CLICKHOUSE_DIALECT, ClickhouseQuery),
        True: (CLICKHOUSE_NATIVE_JSON_DIALECT, ClickhouseNativeJsonQuery),
    }

    @classmethod
    def get(cls, stores_json_natively: bool) -> tuple[ClickhouseDialect, type[Query]]:
        """The dialect of a connection, and the query class rendering in it.

        Args:
            stores_json_natively: Whether a ``JSONField`` is a ``JSON`` column.

        Returns:
            The dialect and the query class.
        """
        return cls.VARIANTS[stores_json_natively]
