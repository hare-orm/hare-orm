from __future__ import annotations

from enum import StrEnum


class SearchType(StrEnum):
    """How a ``SearchQuery`` reads its search text."""

    #: Every word, anywhere.
    PLAIN = "plain"
    #: The words next to each other, in order.
    PHRASE = "phrase"
    #: The database's own query syntax, as it is - PostgreSQL's tsquery, SQLite's FTS5 query.
    RAW = "raw"
    #: Words, ``"quoted phrases"``, ``or`` between alternatives and ``-word`` for a word that
    #: mustn't be there - as a web search engine reads them.
    WEBSEARCH = "websearch"


class SearchOperator(StrEnum):
    """How ``CombinedSearchQuery`` joins two queries."""

    #: Both match - ``query & query``.
    AND = "and"
    #: Either matches - ``query | query``.
    OR = "or"
