"""SQLite's own part of full-text search - FTS5 through the model's ``FullTextIndex``; the search
expressions are ``hare.search``'s."""

from __future__ import annotations

from hare.dialects.sqlite.search.sqlite_text_search import SqliteTextSearch

__all__ = ["SqliteTextSearch"]
