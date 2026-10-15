from __future__ import annotations

from collections.abc import Callable

from hare.dialects.postgresql.enums import PostgresqlRegexMatching
from hare.query.filters.lookups.lookups import Lookups
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.term import Term


class PostgresqlRegexLookups:
    """The PostgreSQL operators of POSIX regular expression lookups."""

    @staticmethod
    def posix_regex(term: Term, value: str, text_function: Callable[[Term], Term] | None = None) -> BasicCriterion:
        return Lookups.regex_criterion(PostgresqlRegexMatching.POSIX_REGEX, term, value, text_function)

    @staticmethod
    def insensitive_posix_regex(
        term: Term, value: str, text_function: Callable[[Term], Term] | None = None
    ) -> BasicCriterion:
        return Lookups.regex_criterion(PostgresqlRegexMatching.IPOSIX_REGEX, term, value, text_function)
