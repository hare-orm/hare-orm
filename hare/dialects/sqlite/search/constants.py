from __future__ import annotations

from hare.lazy_loading.lazy_pattern import LazyPattern

#: The SQLite UDF turning search text into an FTS5 query: ``(text, search type, column filter)``.
SQLITE_FULL_TEXT_QUERY_FUNCTION_NAME = "hare_full_text_query"

#: An FTS5 query matching no row - an empty phrase. FTS5 refuses an empty query.
SQLITE_FULL_TEXT_NO_MATCH_QUERY = '""'

#: The rank of a row the search query doesn't match - as PostgreSQL's ``ts_rank()`` gives it.
SQLITE_FULL_TEXT_UNMATCHED_RANK_SQL = "0.0"

#: The defaults of ``SearchHeadline`` - PostgreSQL's ``ts_headline()`` ones.
SQLITE_FULL_TEXT_DEFAULT_START_SELECTION = "<b>"

SQLITE_FULL_TEXT_DEFAULT_STOP_SELECTION = "</b>"

SQLITE_FULL_TEXT_DEFAULT_FRAGMENT_DELIMITER = " ... "

#: The most words an FTS5 ``snippet()`` returns.
SQLITE_FULL_TEXT_SNIPPET_MAX_WORDS = 64

#: The FTS5 operator joining two queries, keyed by ``SearchOperator`` values.
SQLITE_FULL_TEXT_QUERY_OPERATORS: dict[str, str] = {"and": "AND", "or": "OR"}

#: The FTS5 operator keeping the left query's rows the right one doesn't match.
SQLITE_FULL_TEXT_NOT_OPERATOR = "NOT"

#: A word of WEBSEARCH search text joining the alternatives on either side of it.
SQLITE_FULL_TEXT_OR_WORD = "or"

#: A WEBSEARCH search text token: a quoted phrase, or a run of other characters.
SQLITE_FULL_TEXT_WEBSEARCH_TOKEN_PATTERN = LazyPattern(r'(-?)"([^"]*)"?|(\S+)')

#: A word of search text - a run of letters and digits.
SQLITE_FULL_TEXT_WORD_PATTERN = LazyPattern(r"[^\W_]+")
