from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.exceptions import QueryError
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.subqueries.declarations import KeyRowsQuery
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql import Table

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.expression_context import ExpressionContext


class CteRows(KeyRowsQuery):
    """The rows of a ``WITH`` the queryset attached (``with_cte()``) - ``SELECT <columns> FROM <name>``,
    a value of ``__in``: ``Category.objects.with_cte("ancestors", query).filter(id__in=CteRows("ancestors",
    "id"))``. A composite ``pk__in`` takes as many columns as the key has, in its order.

    Args:
        name: The CTE's name.
        *columns: The CTE's columns selected.

    Raises:
        QueryError: ``name`` or a column isn't a non-empty string, or no column is given.
    """

    plan_parts: ClassVar[DeclaredPlanParts] = (("name", PlanPartType.KEY), ("columns", PlanPartType.KEY))

    def __init__(self, name: str, *columns: str) -> None:
        if not isinstance(name, str) or not name:
            raise QueryError(f"CteRows() takes the CTE's name, got {name!r}")
        if not columns or not all(isinstance(column, str) and column for column in columns):
            raise QueryError(f"CteRows({name!r}) takes the names of the columns it selects, got {columns!r}")
        self.name = name
        self.columns = columns

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        cte = Table(self.name)
        connection = expression_context.connection
        query_class = (
            connection.query_class if connection is not None else expression_context.model._meta.connection.query_class
        )
        rows = query_class.from_(cte).select(*(cte[column] for column in self.columns))
        return ExpressionResult(term=rows, joins=[])
