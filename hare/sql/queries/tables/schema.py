from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.context import SqlContext
from hare.sql.utils import ignore_copy

if TYPE_CHECKING:
    pass
from hare.sql.queries.tables.table import Table


class Schema:
    def __init__(self, name: str, parent: Schema | None = None) -> None:
        self._name = name
        self._parent = parent

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Schema) and self._name == other._name and self._parent == other._parent

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    def _hash_key(self) -> tuple[str, Any]:
        # Schema defines __eq__ (making it unhashable by Python's default rule), and its
        # equality is recursive over _parent - the hash key mirrors that structure exactly
        # so it stays consistent with __eq__.
        return (self._name, self._parent._hash_key() if self._parent else None)

    @ignore_copy
    def __getattr__(self, item: str) -> Table:
        return Table(item, schema=self)

    def get_sql(self, ctx: SqlContext) -> str:
        schema_sql = ctx.quote(self._name)

        if self._parent is not None:
            return f"{self._parent.get_sql(ctx)}.{schema_sql}"

        return schema_sql
