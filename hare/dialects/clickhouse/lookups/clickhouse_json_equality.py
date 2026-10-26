from __future__ import annotations

import operator
from functools import reduce
from typing import Any

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.lookups.constants import CLICKHOUSE_JSON_TYPE_NAME
from hare.dialects.clickhouse.lookups.declarations import ClickhouseStoredJsonText
from hare.exceptions import ValidationError
from hare.query.filters.lookups.lookups import Lookups
from hare.sql.functions.cast import Cast
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class ClickhouseJsonEquality:
    """``JSONField`` equality on a ``JSON`` column - the value cast to ``JSON``, both compared by the text
    ClickHouse writes of them: the column holds an integer as an ``UInt64`` or an ``Int64`` by the way it
    was written (an ``INSERT``, a mutation), and two documents of numbers of different types aren't equal.
    ClickHouse takes no ``IN`` of a ``JSON`` column, and casts no other value than an object to it. The
    column holds objects alone - another value is refused, as a write of it is."""

    @staticmethod
    def raise_if_not_an_object(value: Any) -> None:
        """Refuses a compared value the column can't hold - the JSON text of a number, a string, an array
        or a boolean; an expression goes as it is.

        Args:
            value: The compared value.

        Raises:
            ValidationError: The value is no object.
        """
        if isinstance(value, str) and not value.lstrip().startswith("{"):
            raise ValidationError(f"A JSON column of ClickHouse holds an object, got {value}")

    @staticmethod
    def get_json_term(value: Any) -> Term:
        """The compared JSON text cast to ``JSON``."""
        # The type's name as ClickHouse spells it - a type name given as text is written in capitals.
        return Cast(value if isinstance(value, Term) else ValueWrapper(value), RawSQLTerm(CLICKHOUSE_JSON_TYPE_NAME))

    @classmethod
    def get_compared_texts(cls, term: Term, value: Any) -> tuple[Term, Term]:
        """The column and the compared value as the text ClickHouse writes of a ``JSON`` value.

        Args:
            term: The column.
            value: The compared value.

        Returns:
            Both texts.
        """
        return Function("toString", term), Function("toString", cls.get_json_term(value))

    @classmethod
    def equal(cls, term: Term, value: Any) -> Criterion:
        """``field=value`` on a ``JSONField`` - a field keeping its text compared as that text."""
        if value is None:
            return term.isnull()
        if isinstance(value, ClickhouseStoredJsonText):
            return term == value
        cls.raise_if_not_an_object(value)
        column_text, value_text = cls.get_compared_texts(term, value)
        return column_text == value_text

    @classmethod
    def not_equal(cls, term: Term, value: Any) -> Criterion:
        """``field__not=value`` on a ``JSONField`` - a field keeping its text compared as that text."""
        if value is None:
            return term.notnull()
        if isinstance(value, ClickhouseStoredJsonText):
            return Lookups.not_equal(term, value)
        cls.raise_if_not_an_object(value)
        column_text, value_text = cls.get_compared_texts(term, value)
        return Lookups.not_equal(column_text, value_text)

    @classmethod
    def is_in(cls, term: Term, value: Any) -> Criterion:
        """``field__in=[...]`` on a ``JSONField`` - an equality of each value: no ``IN`` takes a ``JSON``
        column."""
        if not isinstance(value, (list, tuple, set)):
            return Lookups.is_in(term, value)
        criteria = [cls.equal(term, element) for element in value if element is not None]
        return reduce(operator.or_, criteria) if criteria else Lookups.is_in(term, [])

    @classmethod
    def not_in(cls, term: Term, value: Any) -> Criterion:
        """``field__not_in=[...]`` on a ``JSONField`` - see ``is_in()``."""
        if not isinstance(value, (list, tuple, set)):
            return Lookups.not_in(term, value)
        criteria = [cls.not_equal(term, element) for element in value if element is not None]
        return reduce(operator.and_, criteria) if criteria else Lookups.not_in(term, [])
