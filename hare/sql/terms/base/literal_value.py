from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    pass


from typing import TYPE_CHECKING

from hare.sql.context import SqlContext

if TYPE_CHECKING:
    pass
from hare.sql.terms.base.term import Term


class LiteralValue(Term):
    # A constant, like ValueWrapper - abstains from the aggregate vote, so ROUND(AVG(x), 1) is
    # still an aggregate.
    is_aggregate = None

    def __init__(self, value, alias: str | None = None) -> None:
        super().__init__(alias)
        self._value = value

    def get_sql(self, ctx: SqlContext) -> str:
        return ctx.format_alias_sql(self._value, self.alias)


class NullValue(LiteralValue):
    def __init__(self, alias: str | None = None) -> None:
        super().__init__("NULL", alias)
