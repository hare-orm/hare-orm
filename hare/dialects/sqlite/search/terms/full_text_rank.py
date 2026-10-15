from __future__ import annotations

from hare.dialects.sqlite.search.constants import SQLITE_FULL_TEXT_UNMATCHED_RANK_SQL
from hare.dialects.sqlite.search.terms.full_text_row_value import FullTextRowValue
from hare.sql.sql_context import SqlContext
from hare.sql.terms.term import Term


class FullTextRank(FullTextRowValue):
    """How well a row matches the query - FTS5's ``bm25()`` negated, so a better match ranks
    higher; 0 for a row the query doesn't match.

    Args:
        row_key: The column of the row's integer primary key.
        index_table_name: The FTS5 table.
        query: The search query.
        weights: The weight of each column of the index, in its order - empty for 1 each.
    """

    def __init__(
        self,
        row_key: Term,
        index_table_name: str,
        query: Term,
        weights: tuple[float, ...] = (),
        alias: str | None = None,
    ) -> None:
        super().__init__(row_key, index_table_name, query, alias=alias)
        self.weights = weights

    def get_function_sql(self, sql_context: SqlContext, index_table_sql: str) -> str:
        # The weights are checked finite numbers - written as literals.
        weights_sql = "".join(f",{float(weight)!r}" for weight in self.weights)
        return f"-bm25({index_table_sql}{weights_sql})"

    def get_unmatched_value_sql(self, sql_context: SqlContext) -> str | None:
        return SQLITE_FULL_TEXT_UNMATCHED_RANK_SQL
