from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.classes.class_path import ClassPath
from hare.ddl.indexes.ordered_index_key import OrderedIndexKey

if TYPE_CHECKING:
    from hare.sql.sql_context import SqlContext


class CatalogIndexKey(OrderedIndexKey):
    """A complete index key from a catalog, with its observed order kept separately."""

    def get_sql(self, sql_context: SqlContext) -> str:
        return self.term.get_sql(sql_context)

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        return ClassPath.get(type(self)), [self.term, self.order], {}
