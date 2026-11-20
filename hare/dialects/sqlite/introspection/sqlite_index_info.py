from __future__ import annotations

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.sqlite.introspection.catalog_index_key import CatalogIndexKey
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.sql.order import Order


class SqliteIndexInfo(IndexInfo):
    """SQLite reports key order in its catalog and retains complete keys in CREATE INDEX."""

    @staticmethod
    def get_expression_key(sql: str, order: Order) -> CatalogIndexKey:
        return CatalogIndexKey(RawSQLTerm(sql), Order.build(order.is_ascending))
