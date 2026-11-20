from __future__ import annotations

from typing import Any

from hare.sql.builder.tables.table import Table
from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext


class Schema:
    """A schema qualifying a table, possibly qualified by its database."""

    def __init__(self, name: str, parent: Schema | None = None) -> None:
        self._name = name
        self._parent = parent

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Schema) and self._name == other._name and self._parent == other._parent

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash((self._name, self._parent))

    @BuilderMethods.ignore_copy
    def __getattr__(self, item: str) -> Table:
        return Table(item, schema=self)

    def get_sql(self, sql_context: SqlContext) -> str:
        schema_sql = sql_context.quote(self._name)

        if self._parent is not None:
            return f"{self._parent.get_sql(sql_context)}.{schema_sql}"

        return schema_sql
