from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.sqlite.search.terms.full_text_row_value import FullTextRowValue
from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from typing import Self

    from hare.sql.builder.tables.table import Table


class FullTextHeadline(FullTextRowValue):
    """A column's text with the words the query matches marked - FTS5's ``highlight()``, or
    ``snippet()`` of the best fragment with ``max_words``; the text as it is for a row the query
    doesn't match.

    Args:
        row_key: The column of the row's integer primary key.
        index_table_name: The FTS5 table.
        query: The search query.
        column: The highlighted column.
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
        column: Term,
        column_position: int,
        start_selection: str,
        stop_selection: str,
        fragment_delimiter: str,
        max_words: int | None,
        alias: str | None = None,
    ) -> None:
        super().__init__(row_key, index_table_name, query, alias=alias)
        self.column = column
        self.column_position = column_position
        self.start_selection = ValueWrapper(start_selection)
        self.stop_selection = ValueWrapper(stop_selection)
        self.fragment_delimiter = ValueWrapper(fragment_delimiter)
        self.max_words = max_words

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces the table of the row's key column and of the highlighted column.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy with the table replaced.
        """
        self.row_key = self.row_key.replace_table(current_table, new_table)
        self.column = self.column.replace_table(current_table, new_table)
        return self

    def get_value_terms(self) -> tuple[Term, ...]:
        return self.column, self.start_selection, self.stop_selection, self.fragment_delimiter

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

    def get_unmatched_value_sql(self, sql_context: SqlContext) -> str | None:
        return self.column.get_sql(sql_context.copy(with_namespace=True))
