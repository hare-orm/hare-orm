from __future__ import annotations

from typing import Any

from hare.sql.sql_context import SqlContext
from hare.sql.terms.term import Term


class LiteralValue(Term):
    # A constant, like ValueWrapper - abstains from the aggregate vote, so ROUND(AVG(x), 1) is
    # still an aggregate.
    is_aggregate = None

    def __init__(self, value: Any, alias: str | None = None) -> None:
        super().__init__(alias)
        self._value = value

    def get_sql(self, sql_context: SqlContext) -> str:
        return sql_context.format_alias_sql(self._value, self.alias)
