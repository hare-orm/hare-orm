from __future__ import annotations

from hare.query.expressions.expression_result import TableCriterionTuple
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.criteria.empty_criterion import EmptyCriterion


class QueryModifier:
    """The WHERE and HAVING criteria and the joins a filter resolves to."""

    def __init__(
        self,
        where_criterion: Criterion | None = None,
        joins: list[TableCriterionTuple] | None = None,
        having_criterion: Criterion | None = None,
        negation_depth: int = 0,
        has_nullable_column: bool = False,
    ) -> None:
        self.where_criterion: Criterion = where_criterion or EmptyCriterion()
        self.joins = joins or []
        self.having_criterion: Criterion = having_criterion or EmptyCriterion()
        self.negation_depth = negation_depth
        #: Whether a criterion reads a column that can be NULL, or an annotation whose nullability
        #: isn't known - __invert__() then keeps the NULL rows.
        self.has_nullable_column = has_nullable_column

    @staticmethod
    def _and(left: Criterion, right: Criterion) -> Criterion:
        if left and not right:
            return left
        return left & right

    @staticmethod
    def _or(left: Criterion, right: Criterion) -> Criterion:
        if left and not right:
            return left
        return left | right

    def __and__(self, other: QueryModifier) -> QueryModifier:
        return self.__class__(
            where_criterion=QueryModifier._and(self.where_criterion, other.where_criterion),
            joins=self.joins + other.joins,
            having_criterion=QueryModifier._and(self.having_criterion, other.having_criterion),
            negation_depth=max(self.negation_depth, other.negation_depth),
            has_nullable_column=self.has_nullable_column or other.has_nullable_column,
        )

    def _and_criterion(self) -> Criterion:
        return QueryModifier._and(self.where_criterion, self.having_criterion)

    def __or__(self, other: QueryModifier) -> QueryModifier:
        where_criterion = having_criterion = None
        if self.having_criterion or other.having_criterion:
            having_criterion = QueryModifier._or(self._and_criterion(), other._and_criterion())
        else:
            where_criterion = (
                (self.where_criterion | other.where_criterion)
                if self.where_criterion and other.where_criterion
                else (self.where_criterion or other.where_criterion)
            )
        return self.__class__(
            where_criterion,
            self.joins + other.joins,
            having_criterion,
            max(self.negation_depth, other.negation_depth),
            has_nullable_column=self.has_nullable_column or other.has_nullable_column,
        )

    def __invert__(self) -> QueryModifier:
        where_criterion: Criterion | None = None
        having_criterion: Criterion | None = None
        if self.having_criterion:
            # The same IS NOT TRUE rewrite as the WHERE branch below: an aggregate over an empty
            # group (SUM/MAX of no rows) is NULL, and a plain NOT would drop that group too.
            negated_criterion = self.where_criterion & self.having_criterion
            having_criterion = (
                negated_criterion.is_not_true() if self.has_nullable_column else negated_criterion.negate()
            )
        elif self.where_criterion:
            # `NOT (...)` is UNKNOWN for a row whose column is NULL and would drop it; exclude()
            # keeps every row the condition isn't TRUE for - `(...) IS NOT TRUE`. A criterion that
            # can't be NULL gets the plain NOT.
            where_criterion = (
                self.where_criterion.is_not_true() if self.has_nullable_column else self.where_criterion.negate()
            )
        else:
            # A condition-free modifier (e.g. from an empty Q()) is a no-op, and so is its
            # negation - like Django, ~Q() matches every row.
            return self.__class__(joins=self.joins, negation_depth=self.negation_depth)
        return self.__class__(where_criterion, self.joins, having_criterion, self.negation_depth)
