from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.sql.context import SqlContext
from hare.sql.enums import Boolean
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:
    from hare.sql.terms.criteria.complex_criterion import ComplexCriterion
    from hare.sql.terms.criteria.empty_criterion import EmptyCriterion

    pass


class Criterion(Term):
    def __and__(self, other: Any) -> ComplexCriterion:
        # Imported here: the modules import each other.
        from hare.sql.terms.criteria.complex_criterion import ComplexCriterion

        return ComplexCriterion(Boolean.AND, self, other)

    def __or__(self, other: Any) -> ComplexCriterion:
        # Imported here: the modules import each other.
        from hare.sql.terms.criteria.complex_criterion import ComplexCriterion

        return ComplexCriterion(Boolean.OR, self, other)

    def __xor__(self, other: Any) -> ComplexCriterion:
        # Imported here: the modules import each other.
        from hare.sql.terms.criteria.complex_criterion import ComplexCriterion

        return ComplexCriterion(Boolean.XOR, self, other)

    @staticmethod
    def any(terms: Iterable[Term] = ()) -> EmptyCriterion:
        # Imported here: the modules import each other.
        from hare.sql.terms.criteria.empty_criterion import EmptyCriterion

        crit = EmptyCriterion()

        for term in terms:
            crit |= term  # type:ignore[assignment]

        return crit

    @staticmethod
    def all(terms: Iterable[Any] = ()) -> EmptyCriterion:
        # Imported here: the modules import each other.
        from hare.sql.terms.criteria.empty_criterion import EmptyCriterion

        crit = EmptyCriterion()

        for term in terms:
            crit &= term

        return crit

    def get_sql(self, ctx: SqlContext) -> str:
        raise NotImplementedError()
