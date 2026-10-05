from __future__ import annotations

from copy import copy

from hare.search.enums import SearchType
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class FullTextQuery(Function):
    """A search text as an FTS5 query - hare's SQLite function turns it into one, so none of the
    text is read as FTS5 syntax unless the search type is ``RAW``.

    Args:
        text: The search text, or a term giving it.
        search_type: How the text is read.
        column_filter: The FTS5 column filter the query is restricted by (``{"title" "body"}``),
            empty for every column of the index.
    """

    requires_dialect_renderer = True

    def __init__(
        self, text: str | Term, search_type: SearchType, column_filter: str = "", alias: str | None = None
    ) -> None:
        super().__init__("FULL_TEXT_QUERY", text, alias=alias)
        self.search_type = search_type
        self.column_filter = column_filter

    def with_column_filter(self, column_filter: str) -> FullTextQuery:
        """A copy restricted to the columns of an FTS5 column filter.

        Args:
            column_filter: The filter, empty for every column of the index.

        Returns:
            The copy.
        """
        restricted_query = copy(self)
        restricted_query.args = list(self.args)
        restricted_query.column_filter = column_filter
        return restricted_query
