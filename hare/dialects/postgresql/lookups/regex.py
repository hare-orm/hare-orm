from collections.abc import Callable

from hare.dialects.postgresql.enums import PostgresqlRegexMatching
from hare.query.filters.lookups import Lookups
from hare.sql.terms.base.term import Term
from hare.sql.terms.criteria.basic_criterion import BasicCriterion


class PostgresqlRegexLookups:
    """The PostgreSQL operators of POSIX regular expression lookups."""

    @staticmethod
    def posix_regex(field: Term, value: str, text_function: Callable[[Term], Term] | None = None) -> BasicCriterion:
        return Lookups.regex_criterion(PostgresqlRegexMatching.POSIX_REGEX, field, value, text_function)

    @staticmethod
    def insensitive_posix_regex(
        field: Term, value: str, text_function: Callable[[Term], Term] | None = None
    ) -> BasicCriterion:
        return Lookups.regex_criterion(PostgresqlRegexMatching.IPOSIX_REGEX, field, value, text_function)
