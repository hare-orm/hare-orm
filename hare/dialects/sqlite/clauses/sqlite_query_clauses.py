from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.clauses.limit_returning_conflict_query_clauses import LimitReturningConflictQueryClauses
from hare.exceptions import UnSupportedError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.features import Features
    from hare.sql.builder.returned_value import ReturnedValue


class SqliteQueryClauses(LimitReturningConflictQueryClauses):
    """SQLite's clauses - ``LIMIT -1`` for an ``OFFSET`` alone, a returned column always under its
    name, ``EXPLAIN QUERY PLAN``."""

    # SQLite accepts no OFFSET without a LIMIT; a negative one means none.
    unbounded_limit_sql: ClassVar[str | None] = "-1"

    def get_returned_value_sql(self, value: ReturnedValue) -> str:
        # SQLite before 3.36 names a returned column of a quoted name with its quotes ("id"), so a
        # plain column is returned under its own name, which every version reads back the same.
        if value.alias_sql is None and value.column_name is not None:
            return f"{value.sql} AS {self.dialect.literals.quote_identifier(value.column_name)}"
        return super().get_returned_value_sql(value)

    def get_explain_sql(
        self, sql: str, output_format: str | None, options: Mapping[str, bool], features: Features
    ) -> str:
        if output_format:
            raise UnSupportedError("SQLite does not support different explain formats")
        if options:
            raise UnSupportedError("SQLite does not support explain options")
        return f"EXPLAIN QUERY PLAN {sql}"
