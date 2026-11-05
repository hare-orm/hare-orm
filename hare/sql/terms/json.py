from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

from hare.sql.sql_context import SqlContext
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.sql.builder.tables.table import Table


class JSON(Term):
    """A JSON value, written as the dialect's string literal of its text."""

    structure_attributes: ClassVar[tuple[str, ...]] = ("value",)
    literal_attributes = ("value",)

    table: Table | None = None

    def __init__(self, value: Any = None, alias: str | None = None) -> None:
        super().__init__(alias)
        self.value = value

    def get_sql(self, sql_context: SqlContext) -> str:
        value = json.dumps(self.value, separators=(",", ":"), ensure_ascii=False)
        sql = sql_context.dialect.literals.get_string_literal_sql(value)
        return sql_context.format_alias_sql(sql, self.alias)
