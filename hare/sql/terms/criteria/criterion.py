from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.sql.enums import Boolean
from hare.sql.sql_context import SqlContext
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.sql.terms.criteria.complex_criterion import ComplexCriterion
    from hare.sql.terms.criteria.empty_criterion import EmptyCriterion


class Criterion(Term):
    #: ``ComplexCriterion`` - it builds on this class, so it is taken on the first combination
    #: rather than imported here; None until then.
    complex_criterion_class: ClassVar[type[ComplexCriterion] | None] = None

    @staticmethod
    def get_complex_criterion_class() -> type[ComplexCriterion]:
        """``ComplexCriterion``, kept on the class once taken.

        Returns:
            The class.
        """
        from hare.sql.terms.criteria.complex_criterion import ComplexCriterion

        Criterion.complex_criterion_class = ComplexCriterion
        return ComplexCriterion

    def __and__(self, other: Any) -> ComplexCriterion:
        complex_criterion_class = Criterion.complex_criterion_class or Criterion.get_complex_criterion_class()
        return complex_criterion_class(Boolean.AND, self, other)

    def __or__(self, other: Any) -> ComplexCriterion:
        complex_criterion_class = Criterion.complex_criterion_class or Criterion.get_complex_criterion_class()
        return complex_criterion_class(Boolean.OR, self, other)

    def __xor__(self, other: Any) -> ComplexCriterion:
        complex_criterion_class = Criterion.complex_criterion_class or Criterion.get_complex_criterion_class()
        return complex_criterion_class(Boolean.XOR, self, other)

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

    def get_sql(self, sql_context: SqlContext) -> str:
        raise NotImplementedError()
