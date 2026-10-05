from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT, CLICKHOUSE_NATIVE_JSON_DIALECT
from hare.sql.builder.queries.query import Query

ClickhouseQuery = DeclaredSubclass.make(
    Query,
    "ClickhouseQuery",
    __package__,
    """A query rendered in ClickHouse's SQL.""",
    SQL_CONTEXT=CLICKHOUSE_DIALECT.sql_context,
)

ClickhouseNativeJsonQuery = DeclaredSubclass.make(
    Query,
    "ClickhouseNativeJsonQuery",
    __package__,
    """A query rendered in ClickHouse's SQL for a server storing JSON natively.""",
    SQL_CONTEXT=CLICKHOUSE_NATIVE_JSON_DIALECT.sql_context,
)
