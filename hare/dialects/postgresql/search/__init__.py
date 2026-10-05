"""PostgreSQL's own part of full-text search - lexemes, weight labels and the tsvector/tsquery SQL
terms; the search expressions are ``hare.search``'s."""

from __future__ import annotations

from hare.dialects.postgresql.search.lexemes.lexeme import Lexeme

__all__ = ["Lexeme"]
