from __future__ import annotations

from hare.query.filters.constants import DEFAULT_LIKE_ESCAPE_CLAUSE, LIKE_ESCAPE_MAP
from hare.sql import SqlContext
from hare.sql.enums import Matching
from hare.sql.terms.base.term import Term
from hare.sql.terms.criteria.basic_criterion import BasicCriterion


class Like(BasicCriterion):
    """
    A Like that supports an ESCAPE clause

    Args:
        left: The left side of the LIKE expression (field/term).
        right: The right side of the LIKE expression (pattern/value).
        alias: Optional alias for the column.
        escape: The escape clause to use. Defaults to DEFAULT_LIKE_ESCAPE_CLAUSE.
    """

    def __init__(
        self,
        left: Term,
        right: Term,
        alias: str | None = None,
        escape: str = DEFAULT_LIKE_ESCAPE_CLAUSE,
    ) -> None:
        super().__init__(Matching.LIKE, left, right, alias=alias)
        self.escape = escape

    def get_sql(self, ctx: SqlContext) -> str:
        sql = super().get_sql(ctx.copy(with_alias=False)) + str(self.escape)
        if ctx.with_alias and self.alias:
            # format_alias_sql uses the dialect's real quote char (ctx.alias_quote_char or
            # ctx.quote_char) instead of a hardcoded double-quote - matters on any dialect that
            # doesn't quote identifiers with " (e.g. MySQL's backtick style).
            return ctx.format_alias_sql(sql, self.alias)
        return sql

    @staticmethod
    def escape_value(val: str) -> str:
        for char, escaped in LIKE_ESCAPE_MAP:
            val = val.replace(char, escaped)
        return val
