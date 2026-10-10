"""PostgreSQL's own part of full-text search - lexemes, weight labels and the tsvector/tsquery SQL
terms; the search expressions are ``hare.search``'s."""

from __future__ import annotations

from hare.dialects.postgresql.search.criterion.declarations import Comp
from hare.dialects.postgresql.search.criterion.search_criterion import SearchCriterion
from hare.dialects.postgresql.search.enums import TsWeight
from hare.dialects.postgresql.search.lexemes.combined_lexeme import CombinedLexeme
from hare.dialects.postgresql.search.lexemes.lexeme import Lexeme
from hare.dialects.postgresql.search.lexemes.lexeme_combinable import LexemeCombinable
from hare.dialects.postgresql.search.postgresql_text_search import PostgresqlTextSearch

__all__ = [
    "Comp",
    "CombinedLexeme",
    "Lexeme",
    "LexemeCombinable",
    "PostgresqlTextSearch",
    "SearchCriterion",
    "TsWeight",
]
