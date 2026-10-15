from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.sqlite.search.constants import SQLITE_FULL_TEXT_QUERY_FUNCTION_NAME
from hare.dialects.sqlite.search.terms.full_text_query import FullTextQuery
from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers


class SqliteSearchRenderers:
    """How SQLite writes the full-text search terms."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the search renderers on SQLite's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(FullTextQuery, cls.render_full_text_query)

    @staticmethod
    def render_full_text_query(query: FullTextQuery, sql_context: SqlContext) -> str:
        """``hare_full_text_query(text,'plain','{"title"}')`` - the text as an FTS5 query."""
        text_sql = Function.get_arg_sql(query.args[0], sql_context)
        search_type_sql = SqlContext.quote_text(query.search_type.value, "'")
        column_filter_sql = SqlContext.quote_text(query.column_filter, "'")
        return f"{SQLITE_FULL_TEXT_QUERY_FUNCTION_NAME}({text_sql},{search_type_sql},{column_filter_sql})"
