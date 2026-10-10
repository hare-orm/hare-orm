from __future__ import annotations

from hare.dialects.clickhouse.constants import CLICKHOUSE_NEVER_NULLABLE_TYPE_PREFIXES
from hare.dialects.clickhouse.types.constants import CLICKHOUSE_LOW_CARDINALITY_PREFIX


class ClickhouseNullableTypes:
    """How ClickHouse writes a type holding NULL - ``Nullable(T)``, inside a ``LowCardinality(...)``
    (``LowCardinality(Nullable(String))``), and not at all for a type that is never ``Nullable`` (a
    container, whose NULL is written as an empty one)."""

    @staticmethod
    def get_nullable_type(type_sql: str) -> str:
        """The type holding NULL too.

        Args:
            type_sql: The type.

        Returns:
            The type.
        """
        if type_sql.startswith(CLICKHOUSE_NEVER_NULLABLE_TYPE_PREFIXES):
            return type_sql
        if type_sql.startswith(CLICKHOUSE_LOW_CARDINALITY_PREFIX):
            inner_type = type_sql[len(CLICKHOUSE_LOW_CARDINALITY_PREFIX) : -1]
            return f"{CLICKHOUSE_LOW_CARDINALITY_PREFIX}Nullable({inner_type}))"
        return f"Nullable({type_sql})"
