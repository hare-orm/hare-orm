from __future__ import annotations

import aiosqlite

from hare.dialects.sqlite.search.constants import (
    SQLITE_FULL_TEXT_NO_MATCH_QUERY,
    SQLITE_FULL_TEXT_OR_WORD,
    SQLITE_FULL_TEXT_QUERY_FUNCTION_NAME,
    SQLITE_FULL_TEXT_WEBSEARCH_TOKEN_PATTERN,
    SQLITE_FULL_TEXT_WORD_PATTERN,
)
from hare.search.enums import SearchType


class SqliteFullTextQuery:
    """Search text as an FTS5 query - the SQLite function a full-text search binds its text
    through, so a word is never read as FTS5 syntax."""

    @staticmethod
    def quote_phrase(text: str) -> str:
        """An FTS5 string - a phrase of the words of ``text``.

        Args:
            text: The text.

        Returns:
            The quoted phrase.
        """
        return '"' + text.replace('"', '""') + '"'

    @classmethod
    def get_phrase(cls, text: str) -> str | None:
        """The phrase of the words of ``text``, None when it has none.

        Args:
            text: The text.

        Returns:
            The quoted phrase.
        """
        words = SQLITE_FULL_TEXT_WORD_PATTERN.findall(text)
        return cls.quote_phrase(" ".join(words)) if words else None

    @classmethod
    def get_plain_query(cls, text: str) -> str | None:
        """Every word of the text, anywhere."""
        words = SQLITE_FULL_TEXT_WORD_PATTERN.findall(text)
        return " ".join(cls.quote_phrase(word) for word in words) if words else None

    @classmethod
    def get_websearch_query(cls, text: str) -> str | None:
        """The text read as a web search engine reads it: alternatives split by ``or``, each one
        the phrases it has without the ones marked ``-``. An alternative of only excluded phrases
        matches nothing - FTS5 has no query of all rows but some."""
        alternatives: list[tuple[list[str], list[str]]] = [([], [])]
        for match in SQLITE_FULL_TEXT_WEBSEARCH_TOKEN_PATTERN.finditer(text):
            excluded_mark, quoted_text, bare_text = match.groups()
            if bare_text is not None and bare_text.lower() == SQLITE_FULL_TEXT_OR_WORD:
                alternatives.append(([], []))
                continue
            if bare_text is not None:
                excluded = bare_text.startswith("-")
                phrase = cls.get_phrase(bare_text[1:] if excluded else bare_text)
            else:
                excluded = bool(excluded_mark)
                phrase = cls.get_phrase(quoted_text)
            if phrase is not None:
                alternatives[-1][1 if excluded else 0].append(phrase)
        queries = [
            "(" + " AND ".join(included) + "".join(f" NOT {phrase}" for phrase in excluded) + ")"
            for included, excluded in alternatives
            if included
        ]
        return " OR ".join(queries) if queries else None

    @classmethod
    def get_query(cls, text: str | None, search_type: str, column_filter: str) -> str:
        """Backs ``SQLITE_FULL_TEXT_QUERY_FUNCTION_NAME``.

        Args:
            text: The search text.
            search_type: A ``SearchType`` value.
            column_filter: The FTS5 column filter (``{"title" "body"}``), empty for every column.

        Returns:
            The FTS5 query - one matching no row for NULL or text without a word.
        """
        if text is None:
            return SQLITE_FULL_TEXT_NO_MATCH_QUERY
        if search_type == SearchType.PHRASE:
            query = cls.get_phrase(text)
        elif search_type == SearchType.WEBSEARCH:
            query = cls.get_websearch_query(text)
        elif search_type == SearchType.RAW:
            query = text if text.strip() else None
        else:
            query = cls.get_plain_query(text)
        if query is None:
            return SQLITE_FULL_TEXT_NO_MATCH_QUERY
        return f"{column_filter} : ({query})" if column_filter else query

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers the query function on ``connection``."""
        await connection.create_function(SQLITE_FULL_TEXT_QUERY_FUNCTION_NAME, 3, cls.get_query, deterministic=True)
