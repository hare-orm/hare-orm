from __future__ import annotations

import enum
from collections.abc import Callable
from functools import partial
from typing import Any, cast

from hare.exceptions import (
    QueryError,
    UnSupportedError,
    ValidationError,
)
from hare.fields.constants import NULL_BYTE_MESSAGE
from hare.query.filters.lookups.dialect_implemented_operators import DialectImplementedOperators
from hare.query.key_columns import KeyColumns
from hare.sql.constants import LIKE_ESCAPE_MAP
from hare.sql.enums import Equality
from hare.sql.functions.cast import Cast
from hare.sql.functions.text.concat import Concat
from hare.sql.functions.text.replace import Replace
from hare.sql.functions.text.upper import Upper
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.criteria.like import Like
from hare.sql.terms.parameters.parameterized_value_wrapper import ParameterizedValueWrapper
from hare.sql.terms.term import Term
from hare.sql.terms.tuple import Tuple
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.sql.types.sql_types import SqlTypes


class Lookups:
    """The comparison operators of filter lookups: each builds the criterion of ``field <lookup> value``."""

    @staticmethod
    def get_in_list_terms(values: list[Any]) -> list[Term]:
        """Wraps each ``IN (...)`` list value as a bound parameter - a value that's already a SQL term
        (e.g. the ``CAST`` an IntField lookup puts around a non-integer number) is kept as-is.

        Args:
            values: The encoded, non-None lookup values.

        Returns:
            One term per value.
        """
        return [value if isinstance(value, Term) else ParameterizedValueWrapper(value) for value in values]

    @staticmethod
    def is_in(term: Term, value: Any) -> Criterion:
        if not value:
            # SQL has no False, so we return 1=0
            return BasicCriterion(
                Equality.EQ,
                ValueWrapper(1, allow_parametrize=False),
                ValueWrapper(0, allow_parametrize=False),
            )
        if not isinstance(value, (list, tuple, set)):
            # A raw subquery/RawSQL/Term used as field__in=<subquery> - not a literal value list at
            # all, so there's no embedded None to strip; mirrors Term.isin()'s own dispatch (which
            # this eventually calls into) between a real value collection and a bare Term.
            return term.isin(value)
        # `x IN (1, NULL)` never matches a NULL column - a None in the list becomes an explicit IS
        # NULL check.
        non_null_values = [item for item in value if item is not None]
        has_none = len(non_null_values) != len(value)
        if not non_null_values:
            return term.isnull()
        criterion = term.isin(Lookups.get_in_list_terms(non_null_values))
        return criterion | term.isnull() if has_none else criterion

    @staticmethod
    def not_in(term: Term, value: Any) -> Criterion:
        if not value:
            # SQL has no True, so we return 1=1
            return BasicCriterion(
                Equality.EQ,
                ValueWrapper(1, allow_parametrize=False),
                ValueWrapper(1, allow_parametrize=False),
            )
        if not isinstance(value, (list, tuple, set)):
            # A subquery or term, not a list. `x NOT IN (...)` is UNKNOWN when the subquery yields a
            # NULL, so the positive IN is wrapped in IS NOT TRUE - true for both FALSE and UNKNOWN.
            return term.isin(value).is_not_true()
        # A None in the list means NULL rows are excluded (`None not in [1, None]` is False) - no IS
        # NULL check is added then.
        non_null_values = [item for item in value if item is not None]
        has_none = len(non_null_values) != len(value)
        if not non_null_values:
            # every element was None - "not in [None, ...]" means "is not null"
            return term.notnull()
        criterion = term.notin(Lookups.get_in_list_terms(non_null_values))
        return criterion if has_none else criterion | term.isnull()

    @staticmethod
    def between(term: Term, value: tuple[Any, Any]) -> Criterion:
        if len(value) != 2:
            raise QueryError(f"__range expects exactly 2 values (lower, upper), got {len(value)}: {value!r}")
        lower, upper = value
        # A None bound makes the range open-ended - `BETWEEN NULL AND x` would match nothing.
        if lower is None and upper is None:
            # No bound at all - every non-NULL value, the same NULL handling a one-sided range's
            # plain comparison already has.
            return term.notnull()
        if lower is None:
            return term.lte(upper)
        if upper is None:
            return term.gte(lower)
        return term.between(lower, upper)

    @staticmethod
    def row_equal(field: Tuple, value: tuple[Any, ...]) -> Criterion:
        """Equality with a composite key: ``field`` is a ``Tuple`` of columns and ``value`` a tuple of
        DB-ready values - ``row_is_in()`` with one row.
        """
        return KeyColumns.row_is_in(field.values, [value])

    @staticmethod
    def row_not_equal(field: Tuple, value: tuple[Any, ...]) -> Criterion:
        """Negation of ``Lookups.row_equal`` - ORs in an ``IS NULL`` check on the join's first target
        column, same reasoning as ``not_equal()``: a LEFT JOIN with no matching related row nulls out
        every joined column together, and "no relation at all" must count as "not equal to value"."""
        return ~KeyColumns.row_is_in(field.values, [value]) | field.values[0].isnull()

    @staticmethod
    def row_is_in(field: Tuple, value: list[tuple[Any, ...]]) -> Criterion:
        """``is_in()`` for a composite key: ``value`` is a list of DB-ready value tuples, with no None
        among them.
        """
        if not value:
            return BasicCriterion(
                Equality.EQ,
                ValueWrapper(1, allow_parametrize=False),
                ValueWrapper(0, allow_parametrize=False),
            )
        return KeyColumns.row_is_in(field.values, value)

    @staticmethod
    def row_not_in(field: Tuple, value: list[tuple[Any, ...]]) -> Criterion:
        """Negation of ``Lookups.row_is_in`` - see ``Lookups.row_not_equal`` for the ``IS NULL``
        reasoning."""
        if not value:
            return BasicCriterion(
                Equality.EQ,
                ValueWrapper(1, allow_parametrize=False),
                ValueWrapper(1, allow_parametrize=False),
            )
        return ~KeyColumns.row_is_in(field.values, value) | field.values[0].isnull()

    @staticmethod
    def not_equal(term: Term, value: Any) -> Criterion:
        if value is None:
            # term.ne(None) now renders as IS NOT NULL (Term.__ne__'s own None special-case), so
            # ORing it with term.isnull() below would cancel out into a tautology (every row is
            # either NULL or NOT NULL) instead of the intended "term IS NOT NULL" semantics.
            return term.notnull()
        if isinstance(value, Term):
            # A term on the right (`field__not=F("other")`): `<>` is UNKNOWN when either side is
            # NULL - IS DISTINCT FROM compares NULL-safely.
            return term.is_distinct_from(value)
        return term.ne(value) | term.isnull()

    @staticmethod
    def is_null(term: Term, value: Any) -> Criterion:
        if not isinstance(value, bool):
            # Checked here too: some callers pass a raw value, not one ValueEncoders.encode_bool
            # checked.
            raise UnSupportedError(f"__isnull expects a bool, got {value!r}")
        return term.isnull() if value else term.notnull()

    @staticmethod
    def not_null(term: Term, value: Any) -> Criterion:
        if not isinstance(value, bool):
            raise UnSupportedError(f"__not_isnull expects a bool, got {value!r}")
        return term.notnull() if value else term.isnull()

    @staticmethod
    def get_like_pattern_text(value: str, *, prefix: bool, suffix: bool) -> str:
        """The escaped ``%pattern%``/``pattern%``/``%pattern`` text of a LIKE lookup - also built for a
        new value by a query run on a plan.

        Raises:
            ValidationError: ``value`` contains a null byte - SQLite would cut the pattern there.
        """
        if "\x00" in value:
            raise ValidationError(NULL_BYTE_MESSAGE)
        return f"{'%' if prefix else ''}{Like.escape_value(value)}{'%' if suffix else ''}"

    @staticmethod
    def _get_like_pattern(
        term: Term,
        value: Any,
        *,
        prefix: bool,
        suffix: bool,
        case_insensitive: bool,
        text_function: Callable[[Term], Term] | None = None,
    ) -> Criterion:
        left = text_function(term) if text_function is not None else Cast(term, SqlTypes.VARCHAR)
        if isinstance(value, Term):
            # A column or an expression: the pattern is built in SQL - its text escaped as a literal
            # value is, then joined with the wildcards.
            text_value = Cast(value, SqlTypes.VARCHAR)
            escaped: Term = text_value
            for character, escaped_character in LIKE_ESCAPE_MAP:
                escaped = Replace(
                    escaped,
                    ValueWrapper(character, allow_parametrize=False),
                    ValueWrapper(escaped_character, allow_parametrize=False),
                )
            parts: list[Term] = [escaped]
            if prefix:
                parts.insert(0, ValueWrapper("%", allow_parametrize=False))
            if suffix:
                parts.append(ValueWrapper("%", allow_parametrize=False))
            term_pattern: Term = Concat(*parts)
            matches = Like(Upper(left), Upper(term_pattern)) if case_insensitive else Like(left, term_pattern)
            # A NULL value matches nothing, as a NULL pattern doesn't - joined, it would be "%%".
            # Checked as text: a parameter of no known type has none in "IS NULL" on PostgreSQL.
            return text_value.notnull() & matches
        pattern = Lookups.get_like_pattern_text(value, prefix=prefix, suffix=suffix)
        if case_insensitive:
            return Like(Upper(left), term.wrap_constant(Upper(pattern)))
        return Like(left, term.wrap_constant(pattern))

    # One shared implementation instead of 6 near-identical functions differing only by wildcard
    # placement and whether an Upper() wrapper is applied.
    contains = partial(_get_like_pattern, prefix=True, suffix=True, case_insensitive=False)

    starts_with = partial(_get_like_pattern, prefix=False, suffix=True, case_insensitive=False)

    ends_with = partial(_get_like_pattern, prefix=True, suffix=False, case_insensitive=False)

    insensitive_contains = partial(_get_like_pattern, prefix=True, suffix=True, case_insensitive=True)

    insensitive_starts_with = partial(_get_like_pattern, prefix=False, suffix=True, case_insensitive=True)

    insensitive_ends_with = partial(_get_like_pattern, prefix=True, suffix=False, case_insensitive=True)

    @staticmethod
    @DialectImplementedOperators.mark
    def search(term: Term, value: str) -> Any:
        # Will be overridden in each executor
        raise UnSupportedError("The search filter operator is not supported by your database backend")

    @staticmethod
    @DialectImplementedOperators.mark
    def posix_regex(term: Term, value: str, text_function: Callable[[Term], Term] | None = None) -> Any:
        """``field__posix_regex=value`` - implemented by each dialect. SQLite matches with Python's
        ``re``, which isn't resistant to catastrophic backtracking: never pass unvalidated user
        input as ``value`` there.
        """
        raise UnSupportedError("The posix_regex filter operator is not supported by your database backend")

    @staticmethod
    @DialectImplementedOperators.mark
    def insensitive_posix_regex(term: Term, value: str, text_function: Callable[[Term], Term] | None = None) -> Any:
        """``field__iposix_regex=value`` - see ``posix_regex``'s own docstring for the same
        SQLite-specific ReDoS caveat, unaffected by the case-insensitive matching this adds."""
        raise UnSupportedError("The insensitive_posix_regex filter operator is not supported by your database backend")

    @staticmethod
    def insensitive_exact(term: Term, value: Any, *, text_function: Callable[[Term], Term] | None = None) -> Criterion:
        right = Upper(Cast(value, SqlTypes.VARCHAR)) if isinstance(value, Term) else Upper(str(value))
        left = text_function(term) if text_function is not None else Cast(term, SqlTypes.VARCHAR)
        return Upper(left).eq(right)

    @staticmethod
    def regex_criterion(
        comparator: enum.Enum, field_term: Term, value: str, text_function: Callable[[Term], Term] | None = None
    ) -> BasicCriterion:
        """Builds ``field_term <comparator> value`` over ``field_term`` as text, for a dialect's regex operator.
        A NULL field_term stays NULL - it matches neither the filter nor its negation.

        Args:
            comparator: The dialect's regex operator.
            field_term: The matched column.
            value: The pattern.
            text_function: The field_term's text, when it isn't a plain ``VARCHAR`` cast.
        """
        term = cast("Term", field_term.wrap_constant(value))
        text_term = text_function(field_term) if text_function is not None else Cast(field_term, SqlTypes.VARCHAR)
        return BasicCriterion(comparator, text_term, term)
