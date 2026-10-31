"""PostgreSQL's own part of full-text search - lexemes, weight labels and the tsvector/tsquery SQL
terms; the search expressions are ``hare.search``'s."""

from __future__ import annotations

from hare.dialects.postgresql.postgresql_dialect import PostgresqlDialect
from hare.dialects.postgresql.search.lexemes.combined_lexeme import CombinedLexeme
from hare.dialects.postgresql.search.lexemes.lexeme import Lexeme
from hare.query.expressions.dialect_expressions import DialectExpressions

__all__ = ["Lexeme"]

# The search expressions only PostgreSQL has - a query on another database refuses each before any SQL;
# lexemes joined with | and & too.
DialectExpressions.register(PostgresqlDialect, (Lexeme, CombinedLexeme))
