from __future__ import annotations

from hare.dialects.sqlite.search.terms.full_text_row_value import FullTextRowValue
from hare.sql.sql_context import SqlContext
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class FullTextHeadline(FullTextRowValue):
    """A column's text with the words the query matches marked - FTS5's ``highlight()``, or
    ``snippet()`` of the best fragment with ``max_words``; NULL for a row the query doesn't match.

    Args:
        row_key: The column of the row's integer primary key.
        index_table_name: The FTS5 table.
        query: The search query.
        column_position: Its position among the index's columns, from 0.
        start_selection: The text put before a matched word.
        stop_selection: The text put after it.
        fragment_delimiter: The text marking text left out of a fragment.
        max_words: The most words of a fragment, None for the whole text.
    """

    def __init__(
        self,
        row_key: Term,
        index_table_name: str,
        query: Term,
        column_position: int,
        start_selection: str,
        stop_selection: str,
        fragment_delimiter: str,
        max_words: int | None,
        index_schema: str | None = None,
        alias: str | None = None,
    ) -> None:
        super().__init__(row_key, index_table_name, query, index_schema, alias=alias)
        self.column_position = column_position
        self.start_selection = ValueWrapper(start_selection)
        self.stop_selection = ValueWrapper(stop_selection)
        self.fragment_delimiter = ValueWrapper(fragment_delimiter)
        self.max_words = max_words

    def get_value_terms(self) -> tuple[Term, ...]:
        return self.start_selection, self.stop_selection, self.fragment_delimiter

    def get_function_sql(self, sql_context: SqlContext, index_table_sql: str) -> str:
        start_sql = self.start_selection.get_sql(sql_context)
        stop_sql = self.stop_selection.get_sql(sql_context)
        if self.max_words is None:
            return f"highlight({index_table_sql},{self.column_position},{start_sql},{stop_sql})"
        delimiter_sql = self.fragment_delimiter.get_sql(sql_context)
        return (
            f"snippet({index_table_sql},{self.column_position},{start_sql},{stop_sql},{delimiter_sql},"
            f"{self.max_words})"
        )
